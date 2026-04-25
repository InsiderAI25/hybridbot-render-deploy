# Shared auth helper — copied into every service folder so each is
# self-contained for `gcloud run deploy --source=services/<name>`.
#
# Two roles:
#   * fetching outbound ID tokens (when this service calls another Cloud Run)
#   * verifying inbound ID tokens (extracting the caller's SA email)
"""Google ID token helpers for service-to-service Cloud Run calls."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import id_token

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 1800
_token_cache: dict[str, tuple[str, float]] = {}


class IdentityTokenError(RuntimeError):
    pass


def fetch_id_token(audience: str) -> str:
    if not audience:
        raise IdentityTokenError("audience is required")
    try:
        return id_token.fetch_id_token(AuthRequest(), audience)
    except Exception as exc:
        raise IdentityTokenError(f"failed to fetch id_token for {audience}: {exc}") from exc


def cached_id_token(audience: str) -> str:
    now = time.time()
    cached = _token_cache.get(audience)
    if cached and cached[1] > now:
        return cached[0]
    token = fetch_id_token(audience)
    _token_cache[audience] = (token, now + _TOKEN_TTL_SECONDS)
    return token


def auth_headers(audience: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {cached_id_token(audience)}"}


async def authed_post(client: httpx.AsyncClient, url: str, *,
                      audience: str | None = None,
                      json: Any = None, timeout: float = 10.0) -> httpx.Response:
    aud = audience or _audience_from_url(url)
    return await client.post(url, json=json, headers=auth_headers(aud), timeout=timeout)


async def authed_get(client: httpx.AsyncClient, url: str, *,
                     audience: str | None = None,
                     params: dict[str, Any] | None = None,
                     timeout: float = 10.0) -> httpx.Response:
    aud = audience or _audience_from_url(url)
    return await client.get(url, params=params, headers=auth_headers(aud), timeout=timeout)


def _audience_from_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


# ── Inbound caller identity ────────────────────────────────────────────


def verify_caller_id_token(authorization_header: str | None,
                           expected_audience: str | None = None) -> dict[str, Any]:
    """Verify a Google-signed OIDC token and return its claims.

    Raises IdentityTokenError on any failure. The header value should be
    `Bearer <token>`. If expected_audience is set, the token's `aud` claim
    must match exactly.
    """
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        raise IdentityTokenError("missing or malformed Authorization header")
    token = authorization_header.split(" ", 1)[1].strip()
    try:
        claims = id_token.verify_oauth2_token(token, AuthRequest())
    except Exception as exc:
        raise IdentityTokenError(f"id_token verification failed: {exc}") from exc
    if expected_audience and claims.get("aud") != expected_audience:
        raise IdentityTokenError(
            f"audience mismatch: got {claims.get('aud')!r}, want {expected_audience!r}"
        )
    return claims


def caller_email(claims: dict[str, Any]) -> str | None:
    """Best-effort extract of the caller's SA email from verified claims."""
    return claims.get("email")
