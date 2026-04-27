"""
Gemini Coach — Vertex AI gemini-3.1-pro-preview wrapper.

Other agents call this when they need real-time coaching, validation, or
fix suggestions. The model lives in Vertex AI; this service is the empire's
single integration point so every coaching call is logged uniformly.

Endpoints:
  GET  /health
  GET  /status
  POST /advise         — agent sends context, gets advice
  POST /review         — agent sends a decision, model validates
  POST /suggest-fix    — agent sends an error trace, model suggests a fix
  POST /coach          — directive in, agent-friendly translation out
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request

import auth as auth_helpers

# Vertex AI imports are deferred — vertexai import side-effects auth, and
# we want the module to import cleanly even when running without ADC (tests).

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("gemini-coach")

SERVICE_VERSION = os.environ.get("K_REVISION", "dev")
GCP_PROJECT = os.environ.get("VERTEX_AI_PROJECT", "genesis-ai-prod-7x2k")
VERTEX_LOCATION = os.environ.get("VERTEX_AI_LOCATION", "global")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")

KNOWN_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("KNOWN_AGENTS", "").split(",")
    if a.strip()
}

# Generation guardrails.
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "20000"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "2048"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3"))


app = FastAPI(title="Gemini Coach", version=SERVICE_VERSION)
_model = None


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _get_model():
    """Lazy-init the Vertex AI generative model. Hot path; cached."""
    global _model
    if _model is not None:
        return _model
    import vertexai
    from vertexai.generative_models import GenerativeModel

    vertexai.init(project=GCP_PROJECT, location=VERTEX_LOCATION)
    _model = GenerativeModel(GEMINI_MODEL)
    return _model


async def _require_known_agent(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if KNOWN_AGENTS and email not in KNOWN_AGENTS:
        raise HTTPException(status_code=403, detail=f"agent unknown: {email}")
    return email or "unknown"


def _generate(prompt: str) -> dict[str, Any]:
    if len(prompt) > MAX_INPUT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"prompt exceeds {MAX_INPUT_CHARS} chars",
        )
    from vertexai.generative_models import GenerationConfig

    model = _get_model()
    config = GenerationConfig(
        temperature=TEMPERATURE,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    try:
        resp = model.generate_content(prompt, generation_config=config)
    except Exception as exc:
        logger.exception("Vertex AI generation failed")
        raise HTTPException(status_code=502, detail=f"vertex generation failed: {exc}") from exc

    # Extract text robustly across vertex SDK versions.
    text = getattr(resp, "text", None)
    if text is None:
        candidates = getattr(resp, "candidates", []) or []
        if candidates:
            parts = getattr(candidates[0].content, "parts", []) or []
            text = "".join(getattr(p, "text", "") for p in parts)
    return {
        "text": text or "",
        "model": GEMINI_MODEL,
        "vertex_location": VERTEX_LOCATION,
        "generated_at": _now_iso(),
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "gemini-coach",
        "revision": SERVICE_VERSION,
        "model": GEMINI_MODEL,
        "vertex_location": VERTEX_LOCATION,
        "timestamp": _now_iso(),
    }


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "service": "gemini-coach",
        "revision": SERVICE_VERSION,
        "model": GEMINI_MODEL,
        "vertex_location": VERTEX_LOCATION,
        "vertex_project": GCP_PROJECT,
        "known_agents": len(KNOWN_AGENTS),
        "max_input_chars": MAX_INPUT_CHARS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "temperature": TEMPERATURE,
        "timestamp": _now_iso(),
    }


@app.post("/advise")
async def advise(request: Request,
                 caller: str = Depends(_require_known_agent)) -> dict[str, Any]:
    body = await request.json()
    context = (body.get("context") or "").strip()
    question = (body.get("question") or "").strip()
    if not context and not question:
        raise HTTPException(status_code=400, detail="context or question required")
    prompt = (
        "You are an empire-internal coach for AI agents. Be concise, decisive,\n"
        "and explain reasoning. Caller: " + caller + "\n\n"
        "Context:\n" + context + "\n\n"
        "Question:\n" + question
    )
    result = _generate(prompt)
    return {"caller": caller, "advice": result["text"], "meta": result}


@app.post("/review")
async def review(request: Request,
                 caller: str = Depends(_require_known_agent)) -> dict[str, Any]:
    body = await request.json()
    decision = (body.get("decision") or "").strip()
    rationale = (body.get("rationale") or "").strip()
    if not decision:
        raise HTTPException(status_code=400, detail="decision required")
    prompt = (
        "Validate this agent decision. Reply with JSON: {\"valid\": bool,\n"
        "\"risks\": [string], \"recommendation\": string}.\n\n"
        f"Caller: {caller}\nDecision:\n{decision}\n\nRationale:\n{rationale}"
    )
    result = _generate(prompt)
    return {"caller": caller, "review": result["text"], "meta": result}


@app.post("/suggest-fix")
async def suggest_fix(request: Request,
                      caller: str = Depends(_require_known_agent)) -> dict[str, Any]:
    body = await request.json()
    error_text = (body.get("error") or "").strip()
    code_excerpt = (body.get("code") or "").strip()
    if not error_text:
        raise HTTPException(status_code=400, detail="error required")
    prompt = (
        "An empire agent hit this error. Suggest a focused, minimal fix.\n"
        "Reply with JSON: {\"diagnosis\": string, \"fix\": string,\n"
        "\"confidence\": 0..1, \"changed_files\": [string]}.\n\n"
        f"Caller: {caller}\nError:\n{error_text}\n\nCode:\n{code_excerpt}"
    )
    result = _generate(prompt)
    return {"caller": caller, "suggestion": result["text"], "meta": result}


@app.post("/coach")
async def coach(request: Request,
                caller: str = Depends(_require_known_agent)) -> dict[str, Any]:
    body = await request.json()
    directive = (body.get("directive") or "").strip()
    target_agent = (body.get("target_agent") or "").strip() or "any"
    if not directive:
        raise HTTPException(status_code=400, detail="directive required")
    prompt = (
        "Translate this human directive into a precise, agent-friendly\n"
        "instruction set the receiving agent can execute without further\n"
        "interpretation. Be explicit about success criteria and constraints.\n\n"
        f"Caller: {caller}\nTarget agent: {target_agent}\nDirective:\n{directive}"
    )
    result = _generate(prompt)
    return {"caller": caller, "target_agent": target_agent,
            "instructions": result["text"], "meta": result}
