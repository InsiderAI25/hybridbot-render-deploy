"""
Domain Control — manages Namecheap + Cloud DNS + Unstoppable Domains.

Two surfaces:
  * traditional DNS (Namecheap + Unstoppable .com domains): live A/CNAME
    upserts via Namecheap API or Cloud DNS, depending on which is
    authoritative for the zone.
  * Web3 records (Unstoppable .crypto / .x / .nft / etc.): record updates
    via the Unstoppable Domains REST API. Web3 wallet-signed transactions
    are NOT performed here — see /web3/update for the model.

Endpoints:
  GET  /health
  GET  /status
  GET  /domains/inventory      — every domain we touch + expiry + nameservers
  POST /domains/renew          — renew via Namecheap (or queue for human)
  GET  /dns/records?zone=      — list records in a Cloud DNS zone
  POST /dns/upsert             — create or update an A/AAAA/CNAME record
  POST /web3/update            — set IPFS hash / redirect URL on an
                                  Unstoppable Web3 domain via their API
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

import auth as auth_helpers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("domain-control")

SERVICE_VERSION = os.environ.get("K_REVISION", "dev")

# Caller allowlist
ADMIN_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("ADMIN_AGENTS", "").split(",")
    if a.strip()
}

# Namecheap
NAMECHEAP_API_USER = os.environ.get("NAMECHEAP_API_USER", "")
NAMECHEAP_API_KEY = os.environ.get("NAMECHEAP_API_KEY", "")  # via Secret Manager
NAMECHEAP_USERNAME = os.environ.get("NAMECHEAP_USERNAME", "")
NAMECHEAP_CLIENT_IP = os.environ.get("NAMECHEAP_CLIENT_IP", "")  # whitelisted in Namecheap
NAMECHEAP_BASE = os.environ.get("NAMECHEAP_BASE", "https://api.namecheap.com/xml.response")

# Unstoppable Domains
UNSTOPPABLE_API_KEY = os.environ.get("UNSTOPPABLE_API_KEY", "")
UNSTOPPABLE_BASE = os.environ.get(
    "UNSTOPPABLE_BASE", "https://api.unstoppabledomains.com/resolve",
)

# Cloud DNS
CLOUD_DNS_PROJECT = os.environ.get("CLOUD_DNS_PROJECT", "")

# Domains we manage
MANAGED_DOMAINS = [
    d.strip()
    for d in os.environ.get("MANAGED_DOMAINS", "").split(",")
    if d.strip()
]

# Tracker so /status reports degraded mode rather than crashing.
NAMECHEAP_CONFIGURED = bool(
    NAMECHEAP_API_USER and NAMECHEAP_API_KEY and NAMECHEAP_USERNAME and NAMECHEAP_CLIENT_IP
)
UNSTOPPABLE_CONFIGURED = bool(UNSTOPPABLE_API_KEY)
CLOUD_DNS_CONFIGURED = bool(CLOUD_DNS_PROJECT)


app = FastAPI(title="Domain Control", version=SERVICE_VERSION)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


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


# ── Namecheap ────────────────────────────────────────────────────────


def _namecheap_params(command: str, **extra: Any) -> dict[str, str]:
    if not NAMECHEAP_CONFIGURED:
        raise HTTPException(
            status_code=503,
            detail="Namecheap API not configured (NAMECHEAP_* env vars missing)",
        )
    params: dict[str, str] = {
        "ApiUser": NAMECHEAP_API_USER,
        "ApiKey": NAMECHEAP_API_KEY,
        "UserName": NAMECHEAP_USERNAME,
        "ClientIp": NAMECHEAP_CLIENT_IP,
        "Command": command,
    }
    params.update({k: str(v) for k, v in extra.items() if v is not None})
    return params


async def _namecheap_call(command: str, **extra: Any) -> ET.Element:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(NAMECHEAP_BASE, params=_namecheap_params(command, **extra))
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Namecheap {command}: HTTP {resp.status_code}",
        )
    root = ET.fromstring(resp.content)
    if root.attrib.get("Status") != "OK":
        errors = [e.text or "" for e in root.findall(".//Errors/Error")]
        raise HTTPException(
            status_code=502, detail=f"Namecheap {command}: {'; '.join(errors)}",
        )
    return root


async def _namecheap_inventory() -> list[dict[str, Any]]:
    if not NAMECHEAP_CONFIGURED:
        return []
    root = await _namecheap_call("namecheap.domains.getList", PageSize="100")
    ns = "{http://api.namecheap.com/xml.response}"
    out: list[dict[str, Any]] = []
    for d in root.findall(f".//{ns}DomainGetListResult/{ns}Domain"):
        out.append({
            "name": d.attrib.get("Name"),
            "expires": d.attrib.get("Expires"),
            "is_expired": d.attrib.get("IsExpired") == "true",
            "auto_renew": d.attrib.get("AutoRenew") == "true",
            "registrar": "namecheap",
        })
    return out


# ── Cloud DNS ────────────────────────────────────────────────────────


def _cloud_dns_records(zone: str) -> list[dict[str, Any]]:
    from google.cloud import dns

    client = dns.Client(project=CLOUD_DNS_PROJECT)
    z = client.zone(zone)
    if not z.exists():
        raise HTTPException(status_code=404, detail=f"zone not found: {zone}")
    return [
        {
            "name": r.name, "type": r.record_type, "ttl": r.ttl,
            "rrdatas": list(r.rrdatas),
        }
        for r in z.list_resource_record_sets()
    ]


def _cloud_dns_upsert(zone: str, name: str, rtype: str,
                      rrdatas: list[str], ttl: int) -> dict[str, Any]:
    from google.cloud import dns

    client = dns.Client(project=CLOUD_DNS_PROJECT)
    z = client.zone(zone)
    if not z.exists():
        raise HTTPException(status_code=404, detail=f"zone not found: {zone}")

    fqdn = name if name.endswith(".") else f"{name}."
    existing = None
    for r in z.list_resource_record_sets():
        if r.name == fqdn and r.record_type == rtype:
            existing = r
            break

    changes = z.changes()
    if existing:
        changes.delete_record_set(existing)
    changes.add_record_set(z.resource_record_set(fqdn, rtype, ttl, rrdatas))
    changes.create()
    return {"zone": zone, "name": fqdn, "type": rtype, "rrdatas": rrdatas,
            "replaced": existing is not None, "ttl": ttl}


# ── Unstoppable Domains (Web3) ──────────────────────────────────────


async def _unstoppable_resolve(domain: str) -> dict[str, Any]:
    if not UNSTOPPABLE_CONFIGURED:
        raise HTTPException(status_code=503, detail="UNSTOPPABLE_API_KEY not set")
    headers = {"Authorization": f"Bearer {UNSTOPPABLE_API_KEY}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{UNSTOPPABLE_BASE}/domains/{domain}", headers=headers)
    if resp.status_code == 404:
        return {"domain": domain, "resolved": False}
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Unstoppable resolve {domain}: {resp.status_code} {resp.text[:200]}",
        )
    return resp.json()


# ── Endpoints ───────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "domain-control",
            "revision": SERVICE_VERSION, "timestamp": _now_iso()}


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "service": "domain-control",
        "revision": SERVICE_VERSION,
        "namecheap_configured": NAMECHEAP_CONFIGURED,
        "unstoppable_configured": UNSTOPPABLE_CONFIGURED,
        "cloud_dns_configured": CLOUD_DNS_CONFIGURED,
        "managed_domain_count": len(MANAGED_DOMAINS),
        "admin_count": len(ADMIN_AGENTS),
        "timestamp": _now_iso(),
    }


@app.get("/domains/inventory")
async def domains_inventory(_: str = Depends(_require_admin)) -> dict[str, Any]:
    namecheap = await _namecheap_inventory()
    web3: list[dict[str, Any]] = []
    if UNSTOPPABLE_CONFIGURED:
        for d in MANAGED_DOMAINS:
            try:
                r = await _unstoppable_resolve(d)
                web3.append(r)
            except HTTPException:
                continue
    return {
        "namecheap": namecheap,
        "namecheap_count": len(namecheap),
        "web3_resolved": web3,
        "managed_count": len(MANAGED_DOMAINS),
        "timestamp": _now_iso(),
    }


@app.post("/domains/renew")
async def domains_renew(request: Request,
                        caller: str = Depends(_require_admin)) -> dict[str, Any]:
    body = await request.json()
    domain = (body.get("domain") or "").strip()
    years = int(body.get("years") or 1)
    if not domain:
        raise HTTPException(status_code=400, detail="domain required")
    sld, _, tld = domain.partition(".")
    if not sld or not tld:
        raise HTTPException(status_code=400, detail="domain must be SLD.TLD")
    root = await _namecheap_call(
        "namecheap.domains.renew", DomainName=domain, Years=years,
    )
    ns = "{http://api.namecheap.com/xml.response}"
    result = root.find(f".//{ns}DomainRenewResult")
    return {
        "domain": domain, "years": years, "caller": caller,
        "renewed": result.attrib.get("Renew") == "true" if result is not None else False,
        "transaction_id": result.attrib.get("TransactionID") if result is not None else None,
        "charged_amount": result.attrib.get("ChargedAmount") if result is not None else None,
        "timestamp": _now_iso(),
    }


@app.get("/dns/records")
def dns_records(zone: str = Query(..., min_length=1),
                _: str = Depends(_require_admin)) -> dict[str, Any]:
    if not CLOUD_DNS_CONFIGURED:
        raise HTTPException(status_code=503, detail="CLOUD_DNS_PROJECT not set")
    return {"zone": zone, "records": _cloud_dns_records(zone),
            "timestamp": _now_iso()}


@app.post("/dns/upsert")
async def dns_upsert(request: Request,
                     caller: str = Depends(_require_admin)) -> dict[str, Any]:
    if not CLOUD_DNS_CONFIGURED:
        raise HTTPException(status_code=503, detail="CLOUD_DNS_PROJECT not set")
    body = await request.json()
    zone = (body.get("zone") or "").strip()
    name = (body.get("name") or "").strip()
    rtype = (body.get("type") or "").strip().upper()
    rrdatas = body.get("rrdatas") or []
    ttl = int(body.get("ttl") or 300)
    if not zone or not name or rtype not in {"A", "AAAA", "CNAME", "TXT"}:
        raise HTTPException(
            status_code=400,
            detail="zone, name, type (A|AAAA|CNAME|TXT) required",
        )
    if not isinstance(rrdatas, list) or not rrdatas:
        raise HTTPException(status_code=400, detail="rrdatas must be a non-empty list")
    result = _cloud_dns_upsert(zone, name, rtype, rrdatas, ttl)
    result["caller"] = caller
    result["timestamp"] = _now_iso()
    return result


@app.post("/web3/update")
async def web3_update(request: Request,
                      caller: str = Depends(_require_admin)) -> dict[str, Any]:
    """Update an Unstoppable Web3 record via their REST API.

    NOTE: only metadata records (IPFS hash, HTTP redirect, social links) can
    be set via REST. Blockchain ownership transfers require the wallet that
    owns the domain to sign a transaction — that is intentionally NOT done
    here. Set up a separate wallet-signing flow off-platform for ownership.
    """
    if not UNSTOPPABLE_CONFIGURED:
        raise HTTPException(status_code=503, detail="UNSTOPPABLE_API_KEY not set")
    body = await request.json()
    domain = (body.get("domain") or "").strip()
    records = body.get("records") or {}
    if not domain or not isinstance(records, dict) or not records:
        raise HTTPException(
            status_code=400,
            detail="domain + records (object) required",
        )
    headers = {"Authorization": f"Bearer {UNSTOPPABLE_API_KEY}",
               "Content-Type": "application/json"}
    payload = {"records": records}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.patch(
            f"{UNSTOPPABLE_BASE}/domains/{domain}/records",
            json=payload, headers=headers,
        )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Unstoppable update {domain}: {resp.status_code} {resp.text[:300]}",
        )
    return {"domain": domain, "updated": True, "caller": caller,
            "response": resp.json() if resp.content else {},
            "timestamp": _now_iso()}
