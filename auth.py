"""
Google ID token helpers for service-to-service Cloud Run calls.

When running on Cloud Run with an attached service account, we never need a
JSON key file. `google.auth.default()` returns the runtime credentials and
`google.oauth2.id_token.fetch_id_token` mints an OIDC token whose `aud` claim
matches the target service URL. The receiving Cloud Run service validates that
audience against its own URL.

Locally (for tests), GOOGLE_APPLICATION_CREDENTIALS may be set; the same
helpers work because google-auth picks that up via ADC.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

import httpx
from google.auth.transport.requests import Request as AuthRequest
from google.oauth2 import id_token

logger = logging.getLogger(__name__)


_TOKEN_TTL_SECONDS = 1800  # 30 min — Google ID tokens are valid for 1h, refresh well before


class IdentityTokenError(RuntimeError):
    """Raised when an OIDC token cannot be obtained for a target audience."""


def fetch_id_token(audience: str) -> str:
    """Fetch a Google-signed OIDC token whose `aud` claim equals *audience*.

    `audience` must be the full HTTPS root URL of the target Cloud Run service
    (no path), e.g. ``https://genesis-open-brain-xxxx-uc.a.run.app``.
    """
    if not audience:
        raise IdentityTokenError("audience is required")
    try:
        return id_token.fetch_id_token(AuthRequest(), audience)
    except Exception as exc:
        raise IdentityTokenError(f"failed to fetch id_token for {audience}: {exc}") from exc


# Cache tokens per audience for half their lifetime so we don't mint one per request.
_token_cache: dict[str, tuple[str, float]] = {}


def cached_id_token(audience: str) -> str:
    now = time.time()
    cached = _token_cache.get(audience)
    if cached and cached[1] > now:
        return cached[0]
    token = fetch_id_token(audience)
    _token_cache[audience] = (token, now + _TOKEN_TTL_SECONDS)
    return token


def auth_headers(audience: str) -> dict[str, str]:
    """Return Authorization headers carrying an ID token for *audience*."""
    return {"Authorization": f"Bearer {cached_id_token(audience)}"}


async def authed_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    audience: str | None = None,
    json: Any = None,
    timeout: float = 10.0,
) -> httpx.Response:
    """POST *url* with a Google ID token whose audience defaults to the URL root."""
    aud = audience or _audience_from_url(url)
    headers = auth_headers(aud)
    return await client.post(url, json=json, headers=headers, timeout=timeout)


async def authed_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    audience: str | None = None,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    aud = audience or _audience_from_url(url)
    headers = auth_headers(aud)
    return await client.get(url, params=params, headers=headers, timeout=timeout)


def _audience_from_url(url: str) -> str:
    """Strip path/query/fragment from a Cloud Run URL to get the audience."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))
