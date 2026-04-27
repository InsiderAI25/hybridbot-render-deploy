"""
HybridBot / Monique File Manager — FastAPI entry point.

Cloud Run only. Exposes:

  * GET  /            — banner
  * GET  /health      — liveness probe (also reports MFM scan state)
  * GET  /status      — config + Open Brain reachability snapshot
  * POST /sync-agent  — VIE-gated agent sync, calls Open Brain + Newton with
                        Google ID tokens, logs to Open Brain
  * GET  /empire-status — health-check sweep across the agent registry
  * POST /scan        — trigger MFM Sovereign-Dispatch ingestion (background)
  * GET  /scan/status — current ingestion state

Architecture rules respected:
  * FastAPI + Uvicorn (no Gunicorn anywhere)
  * google.auth.default() identity for cross-service OIDC
  * Region us-central1, Vertex AI location global
  * Gemini default gemini-3.1-pro-preview
  * No hardcoded URLs; everything env-driven via config.py
  * Personal account kheprahel@gmail.com is enforced out of agent reach by
    config.assert_personal_account_not_leaked()
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

import auth as auth_helpers
import config
import open_brain
from monique_file_manager import run_sovereign_dispatch

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("hybridbot")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ────────────────────────────────────────────────────────────────────
# State
# ────────────────────────────────────────────────────────────────────


_scan_state: dict[str, Any] = {
    "status": "idle",
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}
_scan_lock = threading.Lock()


# ────────────────────────────────────────────────────────────────────
# Lifespan: announce startup to Open Brain
# ────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting %s revision=%s project=%s region=%s gemini=%s",
        config.AGENT_NAME, config.SERVICE_VERSION, config.GCP_PROJECT,
        config.GCP_REGION, config.GEMINI_MODEL,
    )
    try:
        await open_brain.memory_store(
            agent_name=config.AGENT_NAME,
            event="service_started",
            data={
                "revision": config.SERVICE_VERSION,
                "project": config.GCP_PROJECT,
                "region": config.GCP_REGION,
                "gemini_model": config.GEMINI_MODEL,
            },
        )
    except Exception:
        logger.exception("startup memory_store failed (non-fatal)")
    yield
    logger.info("Shutting down %s", config.AGENT_NAME)


app = FastAPI(
    title="HybridBot / Monique File Manager",
    version=config.SERVICE_VERSION,
    lifespan=lifespan,
)


# ────────────────────────────────────────────────────────────────────
# Root + health
# ────────────────────────────────────────────────────────────────────


@app.get("/")
def index() -> dict[str, Any]:
    return {
        "service": config.AGENT_NAME,
        "message": "HybridBot Cloud Run Deployment Live",
        "revision": config.SERVICE_VERSION,
        "region": config.GCP_REGION,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    with _scan_lock:
        scan_status = _scan_state["status"]
    return {
        "status": "ok",
        "agent": config.AGENT_NAME,
        "revision": config.SERVICE_VERSION,
        "gemini_model": config.GEMINI_MODEL,
        "vertex_location": config.VERTEX_AI_LOCATION,
        "sovereign_dispatch_mode": config.SOVEREIGN_DISPATCH["mode"],
        "scan_state": scan_status,
        "timestamp": _now_iso(),
    }


@app.get("/status")
async def status_endpoint() -> dict[str, Any]:
    """Deeper status — probes Open Brain and Newton if configured."""
    open_brain_ok = False
    newton_ok = False
    if config.OPEN_BRAIN_URL:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await auth_helpers.authed_get(
                    client, f"{config.OPEN_BRAIN_URL}/health",
                    audience=config.OPEN_BRAIN_URL, timeout=4.0,
                )
                open_brain_ok = resp.status_code < 400
        except Exception as exc:
            logger.warning("Open Brain health probe failed: %s", exc)
    if config.NEWTON_URL:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await auth_helpers.authed_get(
                    client, f"{config.NEWTON_URL}/health",
                    audience=config.NEWTON_URL, timeout=4.0,
                )
                newton_ok = resp.status_code < 400
        except Exception as exc:
            logger.warning("Newton health probe failed: %s", exc)

    return {
        "agent": config.AGENT_NAME,
        "revision": config.SERVICE_VERSION,
        "config": {
            "gcp_project": config.GCP_PROJECT,
            "core_project": config.CORE_PROJECT,
            "prod_project": config.PROD_PROJECT,
            "region": config.GCP_REGION,
            "bq_dataset": config.BQ_DATASET,
            "bq_table": config.BQ_TABLE,
            "gemini_model": config.GEMINI_MODEL,
            "vertex_location": config.VERTEX_AI_LOCATION,
            "vie_floor": config.VIE_CONFIDENCE_FLOOR,
        },
        "dependencies": {
            "open_brain_configured": bool(config.OPEN_BRAIN_URL),
            "open_brain_ok": open_brain_ok,
            "newton_configured": bool(config.NEWTON_URL),
            "newton_ok": newton_ok,
        },
        "agent_registry_size": len(config.agent_registry()),
        "scan_state": dict(_scan_state),
        "timestamp": _now_iso(),
    }


# ────────────────────────────────────────────────────────────────────
# /sync-agent — VIE-gated agent sync with real Open Brain + Newton calls
# ────────────────────────────────────────────────────────────────────


@app.post("/sync-agent")
async def sync_agent(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid JSON body: {exc}",
        ) from exc

    agent_name = (body.get("agent_name") or "").strip()
    action = (body.get("action") or "sync").strip()
    confidence = body.get("confidence")

    if not agent_name:
        raise HTTPException(status_code=400, detail="agent_name is required")
    try:
        confidence = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="confidence must be a number")

    if confidence < config.VIE_CONFIDENCE_FLOOR:
        denial = {
            "approved": False,
            "agent": agent_name,
            "action": action,
            "vie_confidence": confidence,
            "vie_floor": config.VIE_CONFIDENCE_FLOOR,
            "reason": (
                f"VIE confidence {confidence} below floor "
                f"{config.VIE_CONFIDENCE_FLOOR}"
            ),
            "timestamp": _now_iso(),
        }
        await open_brain.memory_store(
            agent_name=config.AGENT_NAME,
            event="sync_denied",
            data=denial,
        )
        return denial

    open_brain_ctx, newton_validated = await asyncio.gather(
        _query_open_brain(agent_name, action),
        _query_newton(agent_name, action),
        return_exceptions=False,
    )

    response = {
        "approved": True,
        "agent": agent_name,
        "action": action,
        "vie_confidence": confidence,
        "monique_ceo_authority": "51%",
        "open_brain": open_brain_ctx,
        "newton_validated": bool(newton_validated.get("validated")),
        "newton_detail": newton_validated,
        "timestamp": _now_iso(),
        "empire": {
            "core_project": config.CORE_PROJECT,
            "prod_project": config.PROD_PROJECT,
            "region": config.GCP_REGION,
            "channel_id": config.TELEGRAM_CHANNEL_ID,
        },
    }
    await open_brain.memory_store(
        agent_name=config.AGENT_NAME,
        event="sync_approved",
        data={
            "agent": agent_name,
            "action": action,
            "vie_confidence": confidence,
            "newton_validated": response["newton_validated"],
        },
    )
    return response


async def _query_open_brain(agent_name: str, action: str) -> dict[str, Any]:
    if not config.OPEN_BRAIN_URL:
        return {"status": "skipped", "reason": "OPEN_BRAIN_URL not set"}
    target = f"{config.OPEN_BRAIN_URL}/query"
    payload = {
        "agent": agent_name,
        "context": f"sync_request:{action}",
        "project": config.PROD_PROJECT,
        "timestamp": _now_iso(),
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await auth_helpers.authed_post(
                client, target, audience=config.OPEN_BRAIN_URL,
                json=payload, timeout=8.0,
            )
        if resp.status_code >= 400:
            logger.warning("Open Brain /query %s: %s", resp.status_code, resp.text[:200])
            return {"status": "error", "code": resp.status_code}
        return resp.json()
    except Exception as exc:
        logger.warning("Open Brain /query unreachable: %s", exc)
        return {"status": "unreachable", "error": str(exc)}


async def _query_newton(agent_name: str, action: str) -> dict[str, Any]:
    if not config.NEWTON_URL:
        return {"status": "skipped", "reason": "NEWTON_URL not set", "validated": False}
    target = f"{config.NEWTON_URL}/query"
    payload = {"agent": agent_name, "context": f"validate_sync:{action}"}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await auth_helpers.authed_post(
                client, target, audience=config.NEWTON_URL,
                json=payload, timeout=8.0,
            )
        if resp.status_code >= 400:
            logger.warning("Newton /query %s: %s", resp.status_code, resp.text[:200])
            return {"status": "error", "code": resp.status_code, "validated": False}
        body = resp.json()
        body.setdefault("validated", False)
        return body
    except Exception as exc:
        logger.warning("Newton /query unreachable: %s", exc)
        return {"status": "unreachable", "error": str(exc), "validated": False}


# ────────────────────────────────────────────────────────────────────
# /empire-status — sweep agents from registry
# ────────────────────────────────────────────────────────────────────


@app.get("/empire-status")
async def empire_status() -> dict[str, Any]:
    registry = config.agent_registry()
    if not registry:
        return {
            "empire": "Genesis AI Empire",
            "monique_ceo_authority": "51%",
            "agents_checked": 0,
            "results": {},
            "note": "AGENT_REGISTRY env not set; nothing to sweep",
            "timestamp": _now_iso(),
        }

    results = await asyncio.gather(
        *(_probe_agent(name, url) for name, url in registry.items())
    )
    by_agent = {name: result for name, result in zip(registry.keys(), results)}
    return {
        "empire": "Genesis AI Empire",
        "monique_ceo_authority": "51%",
        "agents_checked": len(registry),
        "results": by_agent,
        "timestamp": _now_iso(),
    }


async def _probe_agent(name: str, url: str) -> dict[str, Any]:
    health_url = f"{url}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await auth_helpers.authed_get(
                client, health_url, audience=url, timeout=5.0,
            )
        return {
            "agent": name,
            "url": url,
            "status_code": resp.status_code,
            "ok": resp.status_code < 400,
        }
    except Exception as exc:
        return {
            "agent": name,
            "url": url,
            "status_code": None,
            "ok": False,
            "error": str(exc),
        }


# ────────────────────────────────────────────────────────────────────
# /scan — Sovereign-Dispatch ingestion in background thread
# ────────────────────────────────────────────────────────────────────


@app.post("/scan", status_code=202)
def scan() -> JSONResponse:
    with _scan_lock:
        if _scan_state["status"] == "running":
            raise HTTPException(status_code=409, detail="scan already running")
        _scan_state.update({
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": None,
            "result": None,
            "error": None,
        })

    thread = threading.Thread(target=_run_scan_thread, daemon=True, name="mfm-scan")
    thread.start()

    return JSONResponse(
        status_code=202,
        content={
            "message": "Sovereign-Dispatch started",
            "mode": config.SOVEREIGN_DISPATCH["mode"],
            "drive_accounts": config.DWD_WORKSPACE_ACCOUNTS,
            "gmail_accounts": config.OAUTH2_GMAIL_ACCOUNTS,
            "started_at": _scan_state["started_at"],
        },
    )


def _run_scan_thread() -> None:
    try:
        result = run_sovereign_dispatch()
        with _scan_lock:
            _scan_state.update({
                "status": "complete",
                "finished_at": _now_iso(),
                "result": result,
                "error": None,
            })
    except Exception as exc:
        logger.exception("Sovereign-Dispatch failed")
        with _scan_lock:
            _scan_state.update({
                "status": "error",
                "finished_at": _now_iso(),
                "error": str(exc),
            })


@app.get("/scan/status")
def scan_status() -> dict[str, Any]:
    with _scan_lock:
        return dict(_scan_state)


# ────────────────────────────────────────────────────────────────────
# Local entry — never used in Cloud Run (the Dockerfile CMD invokes uvicorn).
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        workers=1,
        reload=False,
    )
