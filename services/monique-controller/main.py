"""
Monique Controller — 12-hour cycle orchestrator.

A long-running cycle is the empire's heartbeat. Every 12 hours, Cloud
Scheduler hits POST /cycle/tick (with an OIDC token bound to a
service-account allowlist). The controller:

  1. Records the new cycle in BigQuery.
  2. Calls each registered agent's /cycle/sync endpoint (best-effort) so
     they refresh state from Open Brain.
  3. Triggers Monique CEO to open any pending governance decisions.
  4. Logs the cycle outcome to TMA audit.

Endpoints:
  GET  /health
  GET  /status
  POST /cycle/tick      — Cloud Scheduler hook (gated by SCHEDULER_INVOKER_SAS)
  GET  /cycle/status    — currently running / last completed cycle
  GET  /cycle/history   — last N completed cycles
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from google.cloud import bigquery
from google.cloud.bigquery import SchemaField

import auth as auth_helpers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("monique-controller")

SERVICE_VERSION = os.environ.get("K_REVISION", "dev")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "genesis-ai-prod-7x2k")
BQ_DATASET = os.environ.get("BQ_DATASET", "genesis_shared_memory")
BQ_CYCLES_TABLE = os.environ.get("BQ_CYCLES_TABLE", "monique_controller_cycles")

# Cloud Scheduler invoker(s) — only these SAs may trigger /cycle/tick.
SCHEDULER_INVOKER_SAS = {
    a.strip().lower()
    for a in os.environ.get("SCHEDULER_INVOKER_SAS", "").split(",")
    if a.strip()
}

# Agent registry — same shape as the MFM service.
_AGENT_REGISTRY_RAW = os.environ.get("AGENT_REGISTRY", "")


def _agent_registry() -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in _AGENT_REGISTRY_RAW.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, url = pair.split("=", 1)
        if name.strip() and url.strip():
            out[name.strip()] = url.strip().rstrip("/")
    return out


OPEN_BRAIN_URL = os.environ.get("OPEN_BRAIN_URL", "").rstrip("/")
TMA_URL = os.environ.get("TMA_URL", "").rstrip("/")
MONIQUE_CEO_URL = os.environ.get("MONIQUE_CEO_URL", "").rstrip("/")

CYCLE_HOURS = int(os.environ.get("CYCLE_HOURS", "12"))


_CYCLES_SCHEMA = [
    SchemaField("cycle_id", "STRING", mode="REQUIRED"),
    SchemaField("started_at", "TIMESTAMP", mode="REQUIRED"),
    SchemaField("finished_at", "TIMESTAMP"),
    SchemaField("agents_pinged", "INTEGER"),
    SchemaField("agents_ok", "INTEGER"),
    SchemaField("agents_failed", "INTEGER"),
    SchemaField("results_json", "STRING"),
    SchemaField("status", "STRING"),
]


app = FastAPI(title="Monique Controller", version=SERVICE_VERSION)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _bq() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT)


def _ensure_table(client: bigquery.Client) -> bigquery.TableReference:
    ref = bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(BQ_CYCLES_TABLE)
    table = bigquery.Table(ref, schema=_CYCLES_SCHEMA)
    table.time_partitioning = bigquery.TimePartitioning(field="started_at")
    client.create_table(table, exists_ok=True)
    return ref


async def _require_scheduler(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if not SCHEDULER_INVOKER_SAS:
        raise HTTPException(
            status_code=503,
            detail="SCHEDULER_INVOKER_SAS not configured; refusing to tick",
        )
    if email not in SCHEDULER_INVOKER_SAS:
        raise HTTPException(status_code=403, detail=f"not a scheduler invoker: {email}")
    return email


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "monique-controller",
        "revision": SERVICE_VERSION,
        "cycle_hours": CYCLE_HOURS,
        "timestamp": _now_iso(),
    }


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "service": "monique-controller",
        "revision": SERVICE_VERSION,
        "cycle_hours": CYCLE_HOURS,
        "agents_registered": len(_agent_registry()),
        "scheduler_invokers": len(SCHEDULER_INVOKER_SAS),
        "tma_configured": bool(TMA_URL),
        "monique_ceo_configured": bool(MONIQUE_CEO_URL),
        "timestamp": _now_iso(),
    }


@app.post("/cycle/tick")
async def cycle_tick(scheduler: str = Depends(_require_scheduler)) -> dict[str, Any]:
    cycle_id = uuid.uuid4().hex
    started = _now_iso()
    logger.info("Cycle %s starting (invoker=%s)", cycle_id, scheduler)

    registry = _agent_registry()
    results = await asyncio.gather(
        *(_sync_agent(name, url) for name, url in registry.items()),
        return_exceptions=False,
    )
    by_agent = {name: r for name, r in zip(registry.keys(), results)}
    ok = sum(1 for r in results if r.get("ok"))
    failed = len(results) - ok

    finished = _now_iso()
    row = {
        "cycle_id": cycle_id,
        "started_at": started,
        "finished_at": finished,
        "agents_pinged": len(registry),
        "agents_ok": ok,
        "agents_failed": failed,
        "results_json": json.dumps(by_agent, default=str),
        "status": "complete" if failed == 0 else "partial",
    }
    client = _bq()
    table_ref = _ensure_table(client)
    errors = client.insert_rows_json(table_ref, [row])
    if errors:
        logger.error("cycle insert failed: %s", errors)

    await _audit_log(cycle_id, ok, failed, by_agent)

    return {
        "cycle_id": cycle_id,
        "started_at": started,
        "finished_at": finished,
        "agents_pinged": len(registry),
        "agents_ok": ok,
        "agents_failed": failed,
        "status": row["status"],
    }


async def _sync_agent(name: str, url: str) -> dict[str, Any]:
    """Best-effort: call /cycle/sync if the agent has it, else /health."""
    sync_url = f"{url}/cycle/sync"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await auth_helpers.authed_post(
                client, sync_url, audience=url, json={"trigger": "controller-tick"},
                timeout=8.0,
            )
        if resp.status_code == 404:
            # agent doesn't expose /cycle/sync — fall back to a health probe
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await auth_helpers.authed_get(
                    client, f"{url}/health", audience=url, timeout=5.0,
                )
            return {"agent": name, "ok": resp.status_code < 400, "fallback": "health"}
        return {"agent": name, "ok": resp.status_code < 400,
                "status_code": resp.status_code}
    except Exception as exc:
        return {"agent": name, "ok": False, "error": str(exc)}


async def _audit_log(cycle_id: str, ok: int, failed: int,
                     by_agent: dict[str, Any]) -> None:
    if not TMA_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await auth_helpers.authed_post(
                client, f"{TMA_URL}/audit/log", audience=TMA_URL,
                json={
                    "agent": "monique-controller",
                    "event": "cycle_complete",
                    "data": {
                        "cycle_id": cycle_id,
                        "agents_ok": ok,
                        "agents_failed": failed,
                        "results": by_agent,
                    },
                },
                timeout=5.0,
            )
    except Exception:
        logger.exception("TMA audit log failed (non-fatal)")


@app.get("/cycle/status")
def cycle_status() -> dict[str, Any]:
    client = _bq()
    _ensure_table(client)
    rows = list(client.query(
        f"""SELECT cycle_id, started_at, finished_at, agents_pinged,
                   agents_ok, agents_failed, status
            FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_CYCLES_TABLE}`
            ORDER BY started_at DESC LIMIT 1"""
    ).result())
    if not rows:
        return {"status": "never_run"}
    r = rows[0]
    return {
        "cycle_id": r.cycle_id,
        "started_at": r.started_at.isoformat(),
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "agents_pinged": r.agents_pinged,
        "agents_ok": r.agents_ok,
        "agents_failed": r.agents_failed,
        "status": r.status,
    }


@app.get("/cycle/history")
def cycle_history(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    client = _bq()
    _ensure_table(client)
    rows = list(client.query(
        f"""SELECT cycle_id, started_at, finished_at, agents_ok,
                   agents_failed, status
            FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_CYCLES_TABLE}`
            ORDER BY started_at DESC LIMIT @limit""",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]),
    ).result())
    return {
        "cycles": [
            {
                "cycle_id": r.cycle_id,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "agents_ok": r.agents_ok,
                "agents_failed": r.agents_failed,
                "status": r.status,
            }
            for r in rows
        ]
    }
