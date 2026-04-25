"""
Technical Master AI (TMA) — central audit ledger.

Every agent writes significant actions here so the empire has one provable
log. BQ-backed (`genesis_shared_memory.tma_audit_log`). Reads are gated by
caller allowlist; writes are open to any authenticated empire SA but
recorded with the verified caller email so impersonation is impossible.

Endpoints:
  GET  /health
  GET  /status
  POST /audit/log     — log a single audit row
  POST /audit/bulk    — log many rows in one shot
  GET  /audit/query   — search audit history
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from google.cloud import bigquery
from google.cloud.bigquery import SchemaField

import auth as auth_helpers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("tma")

SERVICE_VERSION = os.environ.get("K_REVISION", "dev")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "genesis-ai-prod-7x2k")
BQ_DATASET = os.environ.get("BQ_DATASET", "genesis_shared_memory")
BQ_AUDIT_TABLE = os.environ.get("BQ_AUDIT_TABLE", "tma_audit_log")

# Read-side allowlist. Writes are accepted from any authenticated agent in
# AUDIT_WRITE_PROJECTS (matches Cloud Run's service-account email suffix).
AUDIT_READ_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("AUDIT_READ_AGENTS", "").split(",")
    if a.strip()
}
AUDIT_WRITE_PROJECTS = {
    p.strip()
    for p in os.environ.get("AUDIT_WRITE_PROJECTS", "").split(",")
    if p.strip()
}


_AUDIT_SCHEMA = [
    SchemaField("audit_id", "STRING", mode="REQUIRED"),
    SchemaField("agent", "STRING", mode="REQUIRED"),
    SchemaField("event", "STRING", mode="REQUIRED"),
    SchemaField("severity", "STRING"),         # info | warn | error | critical
    SchemaField("data_json", "STRING"),
    SchemaField("caller_email", "STRING", mode="REQUIRED"),
    SchemaField("logged_at", "TIMESTAMP", mode="REQUIRED"),
]


app = FastAPI(title="Technical Master AI", version=SERVICE_VERSION)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _bq() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT)


def _ensure_table(client: bigquery.Client) -> bigquery.TableReference:
    ref = bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(BQ_AUDIT_TABLE)
    table = bigquery.Table(ref, schema=_AUDIT_SCHEMA)
    table.time_partitioning = bigquery.TimePartitioning(field="logged_at")
    client.create_table(table, exists_ok=True)
    return ref


async def _require_writer(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if not AUDIT_WRITE_PROJECTS:
        raise HTTPException(
            status_code=503,
            detail="AUDIT_WRITE_PROJECTS not configured; refusing writes",
        )
    for project in AUDIT_WRITE_PROJECTS:
        if email.endswith(f"@{project}.iam.gserviceaccount.com"):
            return email
    raise HTTPException(status_code=403, detail=f"writer not in allowed projects: {email}")


async def _require_reader(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if email not in AUDIT_READ_AGENTS:
        raise HTTPException(status_code=403, detail=f"reader not allowlisted: {email}")
    return email


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "technical-master-ai",
            "revision": SERVICE_VERSION, "timestamp": _now_iso()}


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "service": "technical-master-ai",
        "revision": SERVICE_VERSION,
        "audit_table": f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_AUDIT_TABLE}",
        "writers_projects": sorted(AUDIT_WRITE_PROJECTS),
        "readers": len(AUDIT_READ_AGENTS),
        "timestamp": _now_iso(),
    }


def _row_for(payload: dict[str, Any], caller: str) -> dict[str, Any]:
    import uuid
    agent = (payload.get("agent") or "").strip()
    event = (payload.get("event") or "").strip()
    if not agent or not event:
        raise HTTPException(status_code=400, detail="agent and event are required")
    severity = (payload.get("severity") or "info").lower()
    if severity not in {"info", "warn", "error", "critical"}:
        raise HTTPException(status_code=400, detail="severity must be info|warn|error|critical")
    return {
        "audit_id": uuid.uuid4().hex,
        "agent": agent,
        "event": event,
        "severity": severity,
        "data_json": json.dumps(payload.get("data") or {}, default=str),
        "caller_email": caller,
        "logged_at": _now_iso(),
    }


@app.post("/audit/log")
async def audit_log(request: Request,
                    caller: str = Depends(_require_writer)) -> dict[str, Any]:
    body = await request.json()
    row = _row_for(body, caller)
    client = _bq()
    table_ref = _ensure_table(client)
    errors = client.insert_rows_json(table_ref, [row])
    if errors:
        raise HTTPException(status_code=500, detail=f"BQ insert: {errors}")
    return {"audit_id": row["audit_id"], "logged_at": row["logged_at"]}


@app.post("/audit/bulk")
async def audit_bulk(request: Request,
                     caller: str = Depends(_require_writer)) -> dict[str, Any]:
    body = await request.json()
    items = body.get("rows") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="rows[] is required")
    if len(items) > 500:
        raise HTTPException(status_code=413, detail="max 500 rows per call")
    rows = [_row_for(item, caller) for item in items]
    client = _bq()
    table_ref = _ensure_table(client)
    errors = client.insert_rows_json(table_ref, rows)
    if errors:
        raise HTTPException(status_code=500, detail=f"BQ insert: {errors}")
    return {"inserted": len(rows)}


@app.get("/audit/query")
def audit_query(agent: str | None = Query(default=None),
                event: str | None = Query(default=None),
                severity: str | None = Query(default=None),
                limit: int = Query(100, ge=1, le=1000),
                caller: str = Depends(_require_reader)) -> dict[str, Any]:
    client = _bq()
    _ensure_table(client)
    where = []
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    if agent:
        where.append("agent = @agent")
        params.append(bigquery.ScalarQueryParameter("agent", "STRING", agent))
    if event:
        where.append("event = @event")
        params.append(bigquery.ScalarQueryParameter("event", "STRING", event))
    if severity:
        where.append("severity = @severity")
        params.append(bigquery.ScalarQueryParameter("severity", "STRING", severity))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = list(client.query(
        f"""SELECT audit_id, agent, event, severity, data_json,
                   caller_email, logged_at
            FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_AUDIT_TABLE}`
            {where_sql}
            ORDER BY logged_at DESC LIMIT @limit""",
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result())

    return {
        "filters": {"agent": agent, "event": event, "severity": severity},
        "count": len(rows),
        "rows": [
            {
                "audit_id": r.audit_id,
                "agent": r.agent,
                "event": r.event,
                "severity": r.severity,
                "data": json.loads(r.data_json) if r.data_json else {},
                "caller_email": r.caller_email,
                "logged_at": r.logged_at.isoformat(),
            }
            for r in rows
        ],
    }
