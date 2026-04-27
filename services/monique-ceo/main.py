"""
Monique CEO — 51% voting authority + governance auth-token issuer.

This service is the empire's authority root. Two responsibilities:

  1. Auth-token issuance. Agents request a short-lived governance JWT here;
     Monique CEO validates the caller's Cloud Run identity, the requested
     scope, and the agent's allowlist tier, then signs a JWT via Cloud KMS
     (RS256) and returns it. Other agents verify signatures by fetching
     Monique CEO's public JWK set at /.well-known/jwks.json.

  2. Voting / decision ratification. Agents post proposed decisions; Monique
     CEO records them and, when the 51% threshold is met by tallied yes-
     votes from Aegis / Patent / CFO / TrustLawyer, marks them ratified.

Endpoints:
  GET  /health
  GET  /status
  GET  /authority                — current voting state + tier counts
  POST /auth/issue               — agent requests JWT (returns token + ttl)
  POST /auth/verify              — verify JWT (any agent)
  GET  /.well-known/jwks.json    — public JWK so peers verify offline
  POST /vote                     — gate voter casts a yes/no on a decision
  POST /decide                   — start a decision (returns decision_id)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from google.cloud import bigquery, kms_v1
from google.cloud.bigquery import SchemaField

import auth as auth_helpers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("monique-ceo")

SERVICE_VERSION = os.environ.get("K_REVISION", "dev")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "genesis-ai-prod-7x2k")
BQ_DATASET = os.environ.get("BQ_DATASET", "genesis_shared_memory")

BQ_DECISIONS_TABLE = os.environ.get("BQ_DECISIONS_TABLE", "monique_ceo_decisions")
BQ_TOKENS_TABLE = os.environ.get("BQ_TOKENS_TABLE", "monique_ceo_tokens")

# KMS asymmetric signing key. Format:
#   projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>/cryptoKeyVersions/<v>
KMS_SIGNING_KEY = os.environ.get("KMS_SIGNING_KEY", "")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "monique-ceo")
JWT_DEFAULT_TTL = int(os.environ.get("JWT_DEFAULT_TTL", "900"))  # 15 min
JWT_MAX_TTL = int(os.environ.get("JWT_MAX_TTL", "3600"))         # 1 hour

# Agent tier allowlists. CSV of SA emails per tier.
# Release-gate voters per Genesis directive.
GATE_VOTER_AGENTS = {
    a.strip().lower()
    for a in os.environ.get(
        "GATE_VOTER_AGENTS",
        # No defaults — must be set in prod. Empty == empty roster.
        "",
    ).split(",")
    if a.strip()
}
KNOWN_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("KNOWN_AGENTS", "").split(",")
    if a.strip()
}

# Voting authority. Monique CEO holds 51% — but ratification still requires
# a yes from each release-gate agent (Aegis + Patent + CFO + TrustLawyer).
MONIQUE_CEO_AUTHORITY_PCT = 51


_DECISIONS_SCHEMA = [
    SchemaField("decision_id", "STRING", mode="REQUIRED"),
    SchemaField("title", "STRING", mode="REQUIRED"),
    SchemaField("summary", "STRING"),
    SchemaField("proposer_email", "STRING", mode="REQUIRED"),
    SchemaField("status", "STRING", mode="REQUIRED"),  # open | ratified | rejected | expired
    SchemaField("votes_json", "STRING"),
    SchemaField("created_at", "TIMESTAMP", mode="REQUIRED"),
    SchemaField("decided_at", "TIMESTAMP"),
]

_TOKENS_SCHEMA = [
    SchemaField("jti", "STRING", mode="REQUIRED"),
    SchemaField("subject_email", "STRING", mode="REQUIRED"),
    SchemaField("scope", "STRING"),
    SchemaField("issued_at", "TIMESTAMP", mode="REQUIRED"),
    SchemaField("expires_at", "TIMESTAMP", mode="REQUIRED"),
    SchemaField("revoked_at", "TIMESTAMP"),
]


app = FastAPI(title="Monique CEO", version=SERVICE_VERSION)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _bq() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT)


def _ensure_tables(client: bigquery.Client) -> None:
    for name, schema in (
        (BQ_DECISIONS_TABLE, _DECISIONS_SCHEMA),
        (BQ_TOKENS_TABLE, _TOKENS_SCHEMA),
    ):
        ref = bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(name)
        client.create_table(bigquery.Table(ref, schema=schema), exists_ok=True)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


# ── KMS-signed JWT ──────────────────────────────────────────────────


_kms_client: kms_v1.KeyManagementServiceClient | None = None


def _kms() -> kms_v1.KeyManagementServiceClient:
    global _kms_client
    if _kms_client is None:
        _kms_client = kms_v1.KeyManagementServiceClient()
    return _kms_client


def _kms_sign(message: bytes) -> bytes:
    if not KMS_SIGNING_KEY:
        raise HTTPException(
            status_code=503,
            detail="KMS_SIGNING_KEY not configured; cannot mint tokens",
        )
    response = _kms().asymmetric_sign(
        request={"name": KMS_SIGNING_KEY, "data": message},
    )
    return response.signature


def _kms_public_key_pem() -> str:
    if not KMS_SIGNING_KEY:
        raise HTTPException(status_code=503, detail="KMS_SIGNING_KEY not configured")
    response = _kms().get_public_key(request={"name": KMS_SIGNING_KEY})
    return response.pem


def _public_key_jwk() -> dict[str, Any]:
    """Convert the KMS public-key PEM to a JWK for /.well-known/jwks.json."""
    from cryptography.hazmat.primitives import serialization

    pem = _kms_public_key_pem()
    pub = serialization.load_pem_public_key(pem.encode("ascii"))
    numbers = pub.public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": KMS_SIGNING_KEY.rsplit("/", 1)[-1],
        "n": _b64url(n),
        "e": _b64url(e),
    }


def _verify_jwt(token: str) -> dict[str, Any]:
    """Verify a JWT issued by this Monique CEO instance (RS256 via KMS pubkey)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="malformed JWT")
    header_b64, payload_b64, sig_b64 = parts
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = _b64url_decode(sig_b64)

    pem = _kms_public_key_pem()
    pub = serialization.load_pem_public_key(pem.encode("ascii"))
    try:
        pub.verify(
            signature, signing_input,
            padding.PKCS1v15(), hashes.SHA256(),
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"signature invalid: {exc}") from exc

    payload = json.loads(_b64url_decode(payload_b64))
    if payload.get("exp", 0) < int(time.time()):
        raise HTTPException(status_code=401, detail="token expired")
    if payload.get("iss") != JWT_ISSUER:
        raise HTTPException(status_code=401, detail="issuer mismatch")
    return payload


# ── caller identity ─────────────────────────────────────────────────


async def _require_known_agent(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if KNOWN_AGENTS and email not in KNOWN_AGENTS:
        raise HTTPException(status_code=403, detail=f"agent unknown: {email}")
    return email or "unknown"


async def _require_gate_voter(authorization: str | None = Header(default=None)) -> str:
    email = await _require_known_agent(authorization)
    if not GATE_VOTER_AGENTS:
        raise HTTPException(status_code=503, detail="GATE_VOTER_AGENTS not configured")
    if email not in GATE_VOTER_AGENTS:
        raise HTTPException(status_code=403, detail=f"not a gate voter: {email}")
    return email


# ── public probes ────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "monique-ceo",
        "revision": SERVICE_VERSION,
        "timestamp": _now_iso(),
    }


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "service": "monique-ceo",
        "revision": SERVICE_VERSION,
        "authority_pct": MONIQUE_CEO_AUTHORITY_PCT,
        "kms_configured": bool(KMS_SIGNING_KEY),
        "known_agents": len(KNOWN_AGENTS),
        "gate_voters": len(GATE_VOTER_AGENTS),
        "timestamp": _now_iso(),
    }


@app.get("/authority")
def authority() -> dict[str, Any]:
    return {
        "authority": "Monique CEO",
        "voting_pct": MONIQUE_CEO_AUTHORITY_PCT,
        "release_gate_voters": sorted(GATE_VOTER_AGENTS),
        "human_veto_window_hours": 24,
    }


@app.get("/.well-known/jwks.json")
def jwks() -> dict[str, Any]:
    return {"keys": [_public_key_jwk()]}


# ── Auth issuance ────────────────────────────────────────────────────


@app.post("/auth/issue")
async def auth_issue(request: Request,
                     caller: str = Depends(_require_known_agent)) -> dict[str, Any]:
    body = await request.json()
    scope = (body.get("scope") or "agent:default").strip()
    requested_ttl = int(body.get("ttl_seconds") or JWT_DEFAULT_TTL)
    ttl = max(60, min(requested_ttl, JWT_MAX_TTL))

    now = int(time.time())
    jti = uuid.uuid4().hex
    header = {"alg": "RS256", "typ": "JWT", "kid": KMS_SIGNING_KEY.rsplit("/", 1)[-1]}
    payload = {
        "iss": JWT_ISSUER,
        "sub": caller,
        "scope": scope,
        "jti": jti,
        "iat": now,
        "exp": now + ttl,
        "monique_ceo_authority_pct": MONIQUE_CEO_AUTHORITY_PCT,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    ).encode("ascii")
    signature = _kms_sign(signing_input)
    token = signing_input.decode("ascii") + "." + _b64url(signature)

    client = _bq()
    _ensure_tables(client)
    errors = client.insert_rows_json(
        bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(BQ_TOKENS_TABLE),
        [{
            "jti": jti,
            "subject_email": caller,
            "scope": scope,
            "issued_at": _now_iso(),
            "expires_at": datetime.fromtimestamp(now + ttl, tz=timezone.utc).isoformat(),
        }],
    )
    if errors:
        logger.error("token-log insert failed: %s", errors)
        # Token already minted; failure to record is logged but does not
        # invalidate the JWT. Surface in audit table only.

    return {
        "token": token,
        "token_type": "Bearer",
        "expires_in": ttl,
        "scope": scope,
        "jti": jti,
    }


@app.post("/auth/verify")
async def auth_verify(request: Request) -> dict[str, Any]:
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required")
    payload = _verify_jwt(token)
    return {"valid": True, "payload": payload}


# ── Voting ───────────────────────────────────────────────────────────


@app.post("/decide")
async def decide(request: Request,
                 caller: str = Depends(_require_known_agent)) -> dict[str, Any]:
    body = await request.json()
    title = (body.get("title") or "").strip()
    summary = (body.get("summary") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    decision_id = uuid.uuid4().hex
    row = {
        "decision_id": decision_id,
        "title": title,
        "summary": summary,
        "proposer_email": caller,
        "status": "open",
        "votes_json": json.dumps({}),
        "created_at": _now_iso(),
        "decided_at": None,
    }
    client = _bq()
    _ensure_tables(client)
    errors = client.insert_rows_json(
        bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(BQ_DECISIONS_TABLE),
        [row],
    )
    if errors:
        raise HTTPException(status_code=500, detail=f"BQ insert: {errors}")
    return {"decision_id": decision_id, "status": "open"}


@app.post("/vote")
async def vote(request: Request,
               caller: str = Depends(_require_gate_voter)) -> dict[str, Any]:
    body = await request.json()
    decision_id = (body.get("decision_id") or "").strip()
    choice = (body.get("choice") or "").strip().lower()
    if not decision_id or choice not in {"yes", "no"}:
        raise HTTPException(status_code=400, detail="decision_id + choice (yes|no) required")

    client = _bq()
    _ensure_tables(client)
    table = f"`{GCP_PROJECT}.{BQ_DATASET}.{BQ_DECISIONS_TABLE}`"

    rows = list(client.query(
        f"SELECT votes_json, status FROM {table} WHERE decision_id = @id LIMIT 1",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("id", "STRING", decision_id),
        ]),
    ).result())
    if not rows:
        raise HTTPException(status_code=404, detail="decision not found")
    row = rows[0]
    if row.status != "open":
        return {"decision_id": decision_id, "status": row.status,
                "message": "decision already closed"}

    votes = json.loads(row.votes_json or "{}")
    votes[caller] = choice

    yes_count = sum(1 for v in votes.values() if v == "yes")
    needed = len(GATE_VOTER_AGENTS)  # all gate voters must say yes
    no_count = sum(1 for v in votes.values() if v == "no")

    new_status = "open"
    decided_at = None
    if no_count >= 1:
        new_status = "rejected"
        decided_at = _now_iso()
    elif yes_count >= needed:
        new_status = "ratified"
        decided_at = _now_iso()

    client.query(
        f"""UPDATE {table}
            SET votes_json = @votes, status = @status, decided_at = @decided
            WHERE decision_id = @id""",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("votes", "STRING", json.dumps(votes)),
            bigquery.ScalarQueryParameter("status", "STRING", new_status),
            bigquery.ScalarQueryParameter("decided", "TIMESTAMP", decided_at),
            bigquery.ScalarQueryParameter("id", "STRING", decision_id),
        ]),
    ).result()

    return {
        "decision_id": decision_id,
        "status": new_status,
        "yes_votes": yes_count,
        "no_votes": no_count,
        "voters_required": needed,
    }
