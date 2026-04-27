"""
Genesis Open Brain client.

All agents call into Open Brain for memory retrieve/store. The service URL
comes from OPEN_BRAIN_URL (env). Auth is a Google ID token whose audience is
the Open Brain service root URL.

If OPEN_BRAIN_URL is unset, calls become no-ops that log a warning rather than
raising — this keeps local development viable while still surfacing the gap
in logs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

import auth as auth_helpers
import config

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def memory_retrieve(
    agent_name: str,
    *,
    context: str | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Pull this agent's memory snapshot from Open Brain.

    Returns the parsed JSON body on success, or `{"status": "skipped", ...}`
    if Open Brain is not configured / unreachable. Never raises — startup must
    not crash because Open Brain is down.
    """
    url = config.OPEN_BRAIN_URL
    if not url:
        logger.warning("memory_retrieve: OPEN_BRAIN_URL not set; skipping")
        return {"status": "skipped", "reason": "OPEN_BRAIN_URL not configured"}

    target = f"{url.rstrip('/')}/memory/retrieve"
    params = {"agent": agent_name}
    if context:
        params["context"] = context

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await auth_helpers.authed_get(
                client, target, audience=url, params=params, timeout=timeout
            )
        if resp.status_code >= 400:
            logger.warning(
                "memory_retrieve: %s returned %s — %s",
                target, resp.status_code, resp.text[:200]
            )
            return {"status": "error", "code": resp.status_code}
        return resp.json()
    except Exception as exc:
        logger.warning("memory_retrieve: %s unreachable: %s", target, exc)
        return {"status": "unreachable", "error": str(exc)}


async def memory_store(
    agent_name: str,
    event: str,
    data: dict[str, Any] | None = None,
    *,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Persist a significant action to Open Brain. Best-effort, never raises."""
    url = config.OPEN_BRAIN_URL
    if not url:
        logger.warning("memory_store: OPEN_BRAIN_URL not set; skipping (event=%s)", event)
        return {"status": "skipped"}

    target = f"{url.rstrip('/')}/memory/store"
    payload = {
        "agent_name": agent_name,
        "event": event,
        "data": data or {},
        "timestamp": _now_iso(),
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await auth_helpers.authed_post(
                client, target, audience=url, json=payload, timeout=timeout
            )
        if resp.status_code >= 400:
            logger.warning(
                "memory_store: %s returned %s — %s",
                target, resp.status_code, resp.text[:200]
            )
            return {"status": "error", "code": resp.status_code}
        return resp.json() if resp.content else {"status": "ok"}
    except Exception as exc:
        logger.warning("memory_store: %s unreachable: %s", target, exc)
        return {"status": "unreachable", "error": str(exc)}
