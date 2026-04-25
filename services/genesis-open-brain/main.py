"""
Genesis Open Brain — proprietary agent-only memory service.

NOT public. NOT OpenAI-style. Every call must carry a Google ID token whose
caller email is in OPEN_BRAIN_ALLOWED_AGENTS. BQ is the source of truth for
agent memory; this service is a thin contract on top of it.

Endpoints:
  GET  /health
  GET  /status
  POST /memory/store     — append an event for an agent
  GET  /memory/retrieve  — read recent events for an agent
  POST /query            — keyword + recency search across all agents'
                           memory; gated by caller allowlist
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
logger = logging.getLogger("open-brain")

GCP_PROJECT = os.environ.get("GCP_PROJECT", "genesis-ai-prod-7x2k")
BQ_DATASET = os.environ.get("BQ_DATASET", "genesis_shared_memory")
BQ_MEMORIES_TABLE = os.environ.get("BQ_MEMORIES_TABLE", "agent_memories")
SERVICE_VERSION = os.environ.get("K_REVISION", "dev")

# Allowlist: comma-separated SA emails permitted to call this service.
ALLOWED_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("OPEN_BRAIN_ALLOWED_AGENTS", "").split(",")
    if a.strip()
}

# Optional: also allow an "all-empire" mode where any *.iam.gserviceaccount.com
# in a specific project may call. Set OPEN_BRAIN_ALLOWED_PROJECTS to enable.
ALLOWED_PROJECTS = {
    p.strip()
    for p in os.environ.get("OPEN_BRAIN_ALLOWED_PROJECTS", "").split(",")
    if p.strip()
}


_MEMORY_SCHEMA = [
    SchemaField("agent_name", "STRING", mode="REQUIRED"),
    SchemaField("event", "STRING", mode="REQUIRED"),
    SchemaField("data_json", "STRING"),
    SchemaField("caller_email", "STRING"),
    SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
]


app = FastAPI(title="Genesis Open Brain", version=SERVICE_VERSION)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _bq() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT)


def _ensure_table(client: bigquery.Client) -> bigquery.TableReference:
    ref = bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(BQ_MEMORIES_TABLE)
    table = bigquery.Table(ref, schema=_MEMORY_SCHEMA)
    table.time_partitioning = bigquery.TimePartitioning(field="ingested_at")
    client.create_table(table, exists_ok=True)
    return ref


def _is_caller_allowed(email: str | None) -> bool:
    if not email:
        return False
    email_lc = email.lower()
    if email_lc in ALLOWED_AGENTS:
        return True
    for project in ALLOWED_PROJECTS:
        if email_lc.endswith(f"@{project}.iam.gserviceaccount.com"):
            return True
    return False


async def _require_authed_agent(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: verify the bearer token, return caller email.

    Cloud Run with `--no-allow-unauthenticated` already enforces that callers
    have `roles/run.invoker`. This dependency adds an in-app allowlist check
    so a misconfigured IAM binding can't accidentally expose memory.
    """
    if not ALLOWED_AGENTS and not ALLOWED_PROJECTS:
        raise HTTPException(
            status_code=503,
            detail=(
                "OPEN_BRAIN_ALLOWED_AGENTS / OPEN_BRAIN_ALLOWED_PROJECTS not set; "
                "refusing to serve until allowlist is configured."
            ),
        )
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = auth_helpers.caller_email(claims)
    if not _is_caller_allowed(email):
        raise HTTPException(status_code=403, detail=f"agent not allowlisted: {email}")
    return email or "unknown"


# ───────────────────────── public probes ──────────────────────────────


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "genesis-open-brain",
        "revision": SERVICE_VERSION,
        "timestamp": _now_iso(),
    }


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "service": "genesis-open-brain",
        "revision": SERVICE_VERSION,
        "project": GCP_PROJECT,
        "dataset": BQ_DATASET,
        "memories_table": BQ_MEMORIES_TABLE,
        "allowlist_size": len(ALLOWED_AGENTS),
        "allowlist_projects": sorted(ALLOWED_PROJECTS),
        "timestamp": _now_iso(),
    }


# ───────────────────────── memory endpoints ──────────────────────────


@app.post("/memory/store")
async def memory_store(request: Request,
                       caller: str = Depends(_require_authed_agent)) -> dict[str, Any]:
    body = await request.json()
    agent_name = (body.get("agent_name") or "").strip()
    event = (body.get("event") or "").strip()
    data = body.get("data") or {}
    if not agent_name or not event:
        raise HTTPException(status_code=400, detail="agent_name and event are required")

    row = {
        "agent_name": agent_name,
        "event": event,
        "data_json": json.dumps(data, default=str),
        "caller_email": caller,
        "ingested_at": _now_iso(),
    }
    client = _bq()
    table_ref = _ensure_table(client)
    errors = client.insert_rows_json(table_ref, [row])
    if errors:
        logger.error("BQ insert errors: %s", errors)
        raise HTTPException(status_code=500, detail=f"BQ insert failed: {errors}")
    return {"status": "ok", "agent_name": agent_name, "event": event}


@app.get("/memory/retrieve")
def memory_retrieve(agent: str = Query(..., min_length=1),
                    limit: int = Query(50, ge=1, le=500),
                    caller: str = Depends(_require_authed_agent)) -> dict[str, Any]:
    client = _bq()
    _ensure_table(client)
    sql = f"""
        SELECT agent_name, event, data_json, caller_email, ingested_at
        FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_MEMORIES_TABLE}`
        WHERE agent_name = @agent
        ORDER BY ingested_at DESC
        LIMIT @limit
    """
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("agent", "STRING", agent),
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
        ]),
    )
    events = [
        {
            "agent_name": row.agent_name,
            "event": row.event,
            "data": json.loads(row.data_json) if row.data_json else {},
            "caller_email": row.caller_email,
            "ingested_at": row.ingested_at.isoformat(),
        }
        for row in job.result()
    ]
    return {"agent": agent, "count": len(events), "events": events}


@app.post("/query")
async def query(request: Request,
                caller: str = Depends(_require_authed_agent)) -> dict[str, Any]:
    """Keyword search across all agent memory. Proprietary, not generative.

    Body: {"context": "...", "agent": "optional", "limit": 25}
    Returns the most-recent matching events. No LLM call here — Open Brain
    stays a memory service. Synthesis is a different agent's job.
    """
    body = await request.json()
    context = (body.get("context") or "").strip()
    agent = (body.get("agent") or "").strip() or None
    limit = int(body.get("limit") or 25)
    if not context:
        raise HTTPException(status_code=400, detail="context is required")
    limit = max(1, min(limit, 200))

    client = _bq()
    _ensure_table(client)
    where = ["LOWER(event) LIKE @needle OR LOWER(data_json) LIKE @needle"]
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("needle", "STRING", f"%{context.lower()}%"),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    if agent:
        where.append("agent_name = @agent")
        params.append(bigquery.ScalarQueryParameter("agent", "STRING", agent))

    sql = f"""
        SELECT agent_name, event, data_json, caller_email, ingested_at
        FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_MEMORIES_TABLE}`
        WHERE {' AND '.join(where)}
        ORDER BY ingested_at DESC
        LIMIT @limit
    """
    job = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    matches = [
        {
            "agent_name": row.agent_name,
            "event": row.event,
            "data": json.loads(row.data_json) if row.data_json else {},
            "caller_email": row.caller_email,
            "ingested_at": row.ingested_at.isoformat(),
        }
        for row in job.result()
    ]
    return {
        "context": context,
        "agent": agent,
        "count": len(matches),
        "matches": matches,
    }
