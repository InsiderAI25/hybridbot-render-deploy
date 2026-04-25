"""
Billing Survival — the agent that keeps the empire alive financially.

Three real jobs:
  1. Read GCP billing usage (cost out).
  2. Read Stripe revenue (money in).
  3. Snapshot the runway in days at the current burn vs. revenue rate, and
     surface "act now" alerts to Monique CEO when the runway drops below a
     configurable floor.

This service does NOT autonomously pay bills. Paying real money should be
gated by Monique CEO governance. /survival/pay-bill builds a payment
*request* and returns it; another agent (or a human) submits it to Stripe
or to a bank API. Keeps the audit trail human-reviewable while still
giving the empire a single place to see "are we dying or growing?"

Endpoints:
  GET  /health
  GET  /status
  GET  /survival/snapshot       — current cost / revenue / runway
  GET  /survival/forecast       — projected runway under N scenarios
  POST /survival/alert          — record an alert event in BQ + return it
  POST /survival/pay-bill       — propose a payment (Monique CEO ratifies elsewhere)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from google.cloud import bigquery
from google.cloud.bigquery import SchemaField

import auth as auth_helpers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("billing-survival")

SERVICE_VERSION = os.environ.get("K_REVISION", "dev")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "genesis-ai-prod-7x2k")
BQ_DATASET = os.environ.get("BQ_DATASET", "genesis_shared_memory")
BQ_REVENUE_TABLE = os.environ.get("BQ_REVENUE_TABLE", "revenue_events")
BQ_BILLING_TABLE = os.environ.get("BQ_BILLING_TABLE", "gcp_billing_export_v1")  # BQ billing export
BQ_ALERTS_TABLE = os.environ.get("BQ_ALERTS_TABLE", "billing_survival_alerts")

ADMIN_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("ADMIN_AGENTS", "").split(",")
    if a.strip()
}

# Payment integration (read-only by default).
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")  # via Secret Manager
STRIPE_BASE = "https://api.stripe.com/v1"

# Critical thresholds.
RUNWAY_ALERT_DAYS = int(os.environ.get("RUNWAY_ALERT_DAYS", "5"))
RUNWAY_CRITICAL_DAYS = int(os.environ.get("RUNWAY_CRITICAL_DAYS", "2"))
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

MONIQUE_CEO_URL = os.environ.get("MONIQUE_CEO_URL", "").rstrip("/")


_REVENUE_SCHEMA = [
    SchemaField("event_id", "STRING", mode="REQUIRED"),
    SchemaField("source", "STRING", mode="REQUIRED"),     # stripe | manual | other
    SchemaField("amount_usd", "NUMERIC", mode="REQUIRED"),
    SchemaField("agent", "STRING"),
    SchemaField("description", "STRING"),
    SchemaField("occurred_at", "TIMESTAMP", mode="REQUIRED"),
]

_ALERT_SCHEMA = [
    SchemaField("alert_id", "STRING", mode="REQUIRED"),
    SchemaField("severity", "STRING", mode="REQUIRED"),   # info | warn | critical
    SchemaField("kind", "STRING", mode="REQUIRED"),
    SchemaField("data_json", "STRING"),
    SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
]


app = FastAPI(title="Billing Survival", version=SERVICE_VERSION)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _bq() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT)


def _ensure_tables(client: bigquery.Client) -> None:
    for name, schema in (
        (BQ_REVENUE_TABLE, _REVENUE_SCHEMA),
        (BQ_ALERTS_TABLE, _ALERT_SCHEMA),
    ):
        ref = bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(name)
        client.create_table(bigquery.Table(ref, schema=schema), exists_ok=True)


async def _require_admin(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if not ADMIN_AGENTS:
        raise HTTPException(status_code=503, detail="ADMIN_AGENTS not configured")
    if email not in ADMIN_AGENTS:
        raise HTTPException(status_code=403, detail=f"admin not allowlisted: {email}")
    return email


# ── Revenue (Stripe + manual entries in BQ) ─────────────────────────


async def _stripe_charge_total_usd(since_iso: str) -> Decimal:
    """Sum successful Stripe charges since since_iso. Read-only."""
    if not STRIPE_API_KEY:
        return Decimal("0")
    since_ts = int(datetime.fromisoformat(since_iso.replace("Z", "+00:00")).timestamp())
    headers = {"Authorization": f"Bearer {STRIPE_API_KEY}"}
    total = Decimal("0")
    starting_after: str | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            params: dict[str, Any] = {
                "limit": 100,
                "created[gte]": since_ts,
            }
            if starting_after:
                params["starting_after"] = starting_after
            resp = await client.get(
                f"{STRIPE_BASE}/charges", params=params, headers=headers,
            )
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"Stripe charges: {resp.status_code} {resp.text[:200]}",
                )
            body = resp.json()
            for ch in body.get("data", []):
                if ch.get("paid") and ch.get("status") == "succeeded":
                    # Stripe amounts are in the currency's smallest unit
                    cents = ch.get("amount_captured") or ch.get("amount") or 0
                    total += Decimal(cents) / Decimal(100)
            if not body.get("has_more"):
                break
            starting_after = body["data"][-1]["id"] if body.get("data") else None
            if not starting_after:
                break
    return total


def _bq_revenue_total_usd(client: bigquery.Client, since_iso: str) -> Decimal:
    rows = client.query(
        f"""SELECT COALESCE(SUM(amount_usd), 0) AS total
            FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_REVENUE_TABLE}`
            WHERE occurred_at >= TIMESTAMP(@since)""",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("since", "STRING", since_iso),
        ]),
    ).result()
    for r in rows:
        return Decimal(str(r.total or 0))
    return Decimal("0")


# ── GCP cost (BQ billing export) ────────────────────────────────────


def _gcp_cost_total_usd(client: bigquery.Client, since_iso: str) -> Decimal | None:
    """Sum cost in USD from the GCP billing BQ export.

    Returns None if the export table doesn't exist (it's a customer setup
    step, not auto-created). Caller treats None as "billing data
    unavailable, alert with a manual estimate."
    """
    table_id = f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_BILLING_TABLE}"
    try:
        client.get_table(table_id)
    except Exception:
        return None
    rows = client.query(
        f"""SELECT COALESCE(SUM(cost), 0) AS cost_usd
            FROM `{table_id}`
            WHERE _PARTITIONTIME >= TIMESTAMP(@since)
              AND currency = 'USD'""",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("since", "STRING", since_iso),
        ]),
    ).result()
    for r in rows:
        return Decimal(str(r.cost_usd or 0))
    return Decimal("0")


# ── Endpoints ───────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "billing-survival",
            "revision": SERVICE_VERSION, "timestamp": _now_iso()}


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "service": "billing-survival",
        "revision": SERVICE_VERSION,
        "stripe_configured": bool(STRIPE_API_KEY),
        "monique_ceo_configured": bool(MONIQUE_CEO_URL),
        "lookback_days": LOOKBACK_DAYS,
        "alert_runway_days": RUNWAY_ALERT_DAYS,
        "critical_runway_days": RUNWAY_CRITICAL_DAYS,
        "admin_count": len(ADMIN_AGENTS),
        "timestamp": _now_iso(),
    }


@app.get("/survival/snapshot")
async def snapshot(_: str = Depends(_require_admin)) -> dict[str, Any]:
    since = (datetime.now(tz=timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    client = _bq()
    _ensure_tables(client)

    bq_revenue = _bq_revenue_total_usd(client, since)
    stripe_revenue = Decimal("0")
    stripe_error: str | None = None
    if STRIPE_API_KEY:
        try:
            stripe_revenue = await _stripe_charge_total_usd(since)
        except HTTPException as exc:
            stripe_error = exc.detail
        except Exception as exc:
            stripe_error = str(exc)
    revenue_total = bq_revenue + stripe_revenue

    cost_total = _gcp_cost_total_usd(client, since)
    cost_unavailable = cost_total is None
    cost_total = cost_total or Decimal("0")

    daily_burn = (cost_total / Decimal(LOOKBACK_DAYS)) if LOOKBACK_DAYS else Decimal("0")
    daily_revenue = (revenue_total / Decimal(LOOKBACK_DAYS)) if LOOKBACK_DAYS else Decimal("0")
    net_daily = daily_revenue - daily_burn

    runway_days: float | None = None
    if daily_burn > 0:
        # Simplistic: assume current cash on hand is $0 and we need revenue
        # to cover ongoing burn. Override BALANCE_USD env to seed actual cash.
        balance = Decimal(os.environ.get("BALANCE_USD", "0"))
        if net_daily >= 0:
            runway_days = float("inf")
        else:
            runway_days = float(balance / abs(net_daily)) if balance > 0 else 0.0

    return {
        "lookback_days": LOOKBACK_DAYS,
        "since": since,
        "revenue_usd": str(revenue_total),
        "revenue_breakdown": {
            "stripe": str(stripe_revenue),
            "bq_revenue_events": str(bq_revenue),
        },
        "stripe_error": stripe_error,
        "cost_usd": str(cost_total),
        "cost_unavailable": cost_unavailable,
        "daily_burn_usd": str(daily_burn),
        "daily_revenue_usd": str(daily_revenue),
        "net_daily_usd": str(net_daily),
        "runway_days": runway_days,
        "growing": net_daily > 0,
        "timestamp": _now_iso(),
    }


@app.get("/survival/forecast")
async def forecast(scenarios: int = Query(3, ge=1, le=10),
                   _: str = Depends(_require_admin)) -> dict[str, Any]:
    snap = await snapshot(_=_)  # type: ignore[arg-type]
    base_net = Decimal(snap["net_daily_usd"])
    balance = Decimal(os.environ.get("BALANCE_USD", "0"))
    out: list[dict[str, Any]] = []
    multipliers = [Decimal("1.0"), Decimal("0.5"), Decimal("2.0"),
                   Decimal("0.0"), Decimal("-0.5"), Decimal("3.0"),
                   Decimal("0.25"), Decimal("4.0"), Decimal("-1.0"),
                   Decimal("5.0")]
    for m in multipliers[:scenarios]:
        net = base_net * m
        runway: float | None
        if net >= 0:
            runway = float("inf")
        elif balance > 0:
            runway = float(balance / abs(net))
        else:
            runway = 0.0
        out.append({
            "revenue_multiplier": str(m),
            "net_daily_usd": str(net),
            "runway_days": runway,
        })
    return {
        "base": snap, "scenarios": out, "timestamp": _now_iso(),
    }


@app.post("/survival/alert")
async def survival_alert(request: Request,
                         caller: str = Depends(_require_admin)) -> dict[str, Any]:
    body = await request.json()
    severity = (body.get("severity") or "warn").lower()
    if severity not in {"info", "warn", "critical"}:
        raise HTTPException(status_code=400, detail="severity must be info|warn|critical")
    kind = (body.get("kind") or "").strip()
    if not kind:
        raise HTTPException(status_code=400, detail="kind required")
    row = {
        "alert_id": uuid.uuid4().hex,
        "severity": severity,
        "kind": kind,
        "data_json": json.dumps(body.get("data") or {}, default=str),
        "created_at": _now_iso(),
    }
    client = _bq()
    _ensure_tables(client)
    errors = client.insert_rows_json(
        bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(BQ_ALERTS_TABLE),
        [row],
    )
    if errors:
        raise HTTPException(status_code=500, detail=f"BQ insert: {errors}")

    # Best-effort: notify Monique CEO if critical and a URL is configured.
    if severity == "critical" and MONIQUE_CEO_URL:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client_h:
                await auth_helpers.authed_post(
                    client_h, f"{MONIQUE_CEO_URL}/decide",
                    audience=MONIQUE_CEO_URL,
                    json={
                        "title": f"survival-critical:{kind}",
                        "summary": (
                            f"billing-survival raised a critical alert ({kind}); "
                            "agents need governance ratification on response actions"
                        ),
                    },
                    timeout=5.0,
                )
        except Exception:
            logger.exception("Monique CEO /decide call failed (non-fatal)")

    return {"alert_id": row["alert_id"], "severity": severity, "caller": caller,
            "created_at": row["created_at"]}


@app.post("/survival/pay-bill")
async def pay_bill(request: Request,
                   caller: str = Depends(_require_admin)) -> dict[str, Any]:
    """Propose (don't execute) a bill payment.

    Returns a structured `payment_request` that another agent or human must
    submit to Stripe / a bank API. The empire's policy: real-money flows
    require Monique CEO ratification before execution.
    """
    body = await request.json()
    payee = (body.get("payee") or "").strip()
    amount_usd = body.get("amount_usd")
    reason = (body.get("reason") or "").strip()
    if not payee or amount_usd is None or not reason:
        raise HTTPException(status_code=400, detail="payee, amount_usd, reason required")
    try:
        amount = Decimal(str(amount_usd))
    except Exception:
        raise HTTPException(status_code=400, detail="amount_usd must be numeric")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be > 0")

    request_id = uuid.uuid4().hex
    payment_request = {
        "request_id": request_id,
        "payee": payee,
        "amount_usd": str(amount),
        "reason": reason,
        "proposer": caller,
        "status": "proposed",
        "created_at": _now_iso(),
    }

    # Open a Monique CEO decision so gate voters can ratify.
    decision: dict[str, Any] | None = None
    if MONIQUE_CEO_URL:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client_h:
                resp = await auth_helpers.authed_post(
                    client_h, f"{MONIQUE_CEO_URL}/decide",
                    audience=MONIQUE_CEO_URL,
                    json={
                        "title": f"pay-bill:{payee}:{amount}",
                        "summary": reason,
                    },
                    timeout=8.0,
                )
                if resp.status_code < 400:
                    decision = resp.json()
        except Exception as exc:
            logger.warning("Monique CEO /decide failed: %s", exc)
            decision = {"error": str(exc)}

    return {
        "payment_request": payment_request,
        "monique_ceo_decision": decision,
        "next_step": (
            "wait for /vote ratification on monique-ceo decision_id, "
            "then a human or executor agent submits the actual transfer"
        ),
        "timestamp": _now_iso(),
    }
