"""
Generic agent template — copy this folder, rename, fill in.

Default endpoints satisfy the empire's contract every agent must honor:

  GET  /health
  GET  /status
  POST /sync-agent       — Coordinator V2 entry point
  POST /cycle/sync       — Monique Controller cycle hook
  POST /audit/forward    — local helper that forwards to TMA

Agent-specific endpoints (e.g. /predict, /generate, /score) go below the
fold marked "AGENT-SPECIFIC".
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request

import auth as auth_helpers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("agent")

AGENT_NAME = os.environ.get("AGENT_NAME", "unnamed-agent")
SERVICE_VERSION = os.environ.get("K_REVISION", "dev")

OPEN_BRAIN_URL = os.environ.get("OPEN_BRAIN_URL", "").rstrip("/")
TMA_URL = os.environ.get("TMA_URL", "").rstrip("/")

KNOWN_CALLERS = {
    a.strip().lower()
    for a in os.environ.get("KNOWN_CALLERS", "").split(",")
    if a.strip()
}


app = FastAPI(title=AGENT_NAME, version=SERVICE_VERSION)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def _require_caller(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if KNOWN_CALLERS and email not in KNOWN_CALLERS:
        raise HTTPException(status_code=403, detail=f"caller unknown: {email}")
    return email or "unknown"


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "agent": AGENT_NAME,
            "revision": SERVICE_VERSION, "timestamp": _now_iso()}


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "agent": AGENT_NAME,
        "revision": SERVICE_VERSION,
        "open_brain_configured": bool(OPEN_BRAIN_URL),
        "tma_configured": bool(TMA_URL),
        "known_callers": len(KNOWN_CALLERS),
        "timestamp": _now_iso(),
    }


@app.post("/sync-agent")
async def sync_agent(request: Request,
                     caller: str = Depends(_require_caller)) -> dict[str, Any]:
    body = await request.json()
    action = (body.get("action") or "").strip()
    await _audit("sync_agent", {"action": action, "caller": caller})
    return {"agent": AGENT_NAME, "synced": True, "action": action,
            "caller": caller, "timestamp": _now_iso()}


@app.post("/cycle/sync")
async def cycle_sync(request: Request,
                     caller: str = Depends(_require_caller)) -> dict[str, Any]:
    body = await request.json()
    trigger = (body.get("trigger") or "controller-tick").strip()
    await _audit("cycle_sync", {"trigger": trigger, "caller": caller})
    return {"agent": AGENT_NAME, "cycle_acknowledged": True,
            "trigger": trigger, "timestamp": _now_iso()}


@app.post("/audit/forward")
async def audit_forward(request: Request,
                        caller: str = Depends(_require_caller)) -> dict[str, Any]:
    body = await request.json()
    event = (body.get("event") or "").strip()
    if not event:
        raise HTTPException(status_code=400, detail="event required")
    result = await _audit(event, body.get("data") or {})
    return {"forwarded": True, "result": result}


async def _audit(event: str, data: dict[str, Any]) -> dict[str, Any]:
    if not TMA_URL:
        return {"status": "skipped", "reason": "TMA_URL not configured"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await auth_helpers.authed_post(
                client, f"{TMA_URL}/audit/log", audience=TMA_URL,
                json={"agent": AGENT_NAME, "event": event, "data": data},
                timeout=5.0,
            )
        return {"status": "ok" if resp.status_code < 400 else "error",
                "code": resp.status_code}
    except Exception as exc:
        logger.warning("audit forward failed: %s", exc)
        return {"status": "unreachable", "error": str(exc)}


# ─────────── AGENT-SPECIFIC endpoints below this line ───────────────
# Replace this block with your agent's actual capabilities. Keep them
# authenticated via Depends(_require_caller) and audit every significant
# action via _audit().
