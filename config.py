"""
HybridBot / Monique File Manager configuration.

All values are env-driven so the same image can run on Cloud Run and locally
without code changes. Defaults reflect the production architecture
documented in CLAUDE.md and the Genesis AI Empire directives:

  * Gemini model: gemini-3.1-pro-preview (NEVER 2.5)
  * Vertex AI location: global (required for 3.1)
  * Region: us-central1
  * BigQuery project: genesis-ai-prod-7x2k
  * Dataset: genesis_shared_memory
  * Naming locks (do NOT change):
      - admin@insideraiagency.com  → agent-accessible Workspace
      - sportsai@insidersportsai.com → agent-accessible Workspace
      - kheprahel@gmail.com  → PERSONAL, never granted to agents
"""

from __future__ import annotations

import os


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------- Service identity ----------
AGENT_NAME = os.environ.get("AGENT_NAME", "monique-file-manager")
SERVICE_VERSION = os.environ.get("K_REVISION") or os.environ.get("SERVICE_VERSION", "dev")

# ---------- Vertex AI / Gemini ----------
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")
VERTEX_AI_LOCATION = os.environ.get("VERTEX_AI_LOCATION", "global")
VERTEX_AI_PROJECT = os.environ.get("VERTEX_AI_PROJECT", "genesis-ai-prod-7x2k")

# ---------- GCP projects ----------
GCP_PROJECT = os.environ.get("GCP_PROJECT", "genesis-ai-prod-7x2k")
CORE_PROJECT = os.environ.get("CORE_PROJECT", "insider-core-project")
PROD_PROJECT = os.environ.get("PROD_PROJECT", "genesis-ai-prod-7x2k")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")

# ---------- BigQuery ----------
BQ_DATASET = os.environ.get("BQ_DATASET", "genesis_shared_memory")
# Default table per the stub-elimination directive.
# Existing deployments using "file_catalog" can override via BQ_TABLE.
BQ_TABLE = os.environ.get("BQ_TABLE", "monique_discovered_files")
BQ_AUDIT_TABLE = os.environ.get("BQ_AUDIT_TABLE", "tma_audit_log")

# ---------- Pub/Sub ----------
PUBSUB_TOPIC = os.environ.get(
    "PUBSUB_TOPIC",
    f"projects/{GCP_PROJECT}/topics/genesis-file-manager-complete",
)

# ---------- Open Brain ----------
# Set this to the full HTTPS root of the genesis-open-brain Cloud Run service.
# Empty string disables Open Brain integration (calls become logged no-ops).
OPEN_BRAIN_URL = os.environ.get("OPEN_BRAIN_URL", "").rstrip("/")

# ---------- Newton (CLO validator) ----------
NEWTON_URL = os.environ.get("NEWTON_URL", "").rstrip("/")

# ---------- VIE confidence floor ----------
# Below this, sync requests are rejected without dispatching.
VIE_CONFIDENCE_FLOOR = float(os.environ.get("VIE_CONFIDENCE_FLOOR", "0.77"))

# ---------- Telegram ----------
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

# ---------- Empire roster (env-driven, no hardcoded URLs) ----------
# AGENT_REGISTRY entries take the form "<agent-name>=https://...run.app", comma-separated.
# Example:
#   AGENT_REGISTRY="sharp-og=https://sharp-og-xxxx-uc.a.run.app,newton-ai=https://newton-ai-xxxx-uc.a.run.app"
_AGENT_REGISTRY_RAW = os.environ.get("AGENT_REGISTRY", "")


def agent_registry() -> dict[str, str]:
    """Parse AGENT_REGISTRY env into {name: url}. Empty if unset."""
    out: dict[str, str] = {}
    for pair in _AGENT_REGISTRY_RAW.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, url = pair.split("=", 1)
        name = name.strip()
        url = url.strip().rstrip("/")
        if name and url:
            out[name] = url
    return out


# ---------- Domain-Wide Delegation (Workspace accounts) ----------
# DWD with Drive/Gmail requires either:
#   (a) a service-account JSON key with `subject=user@domain` impersonation, or
#   (b) ADC + impersonated credentials chain to a DWD-enabled SA.
#
# Path (b) is set up via DWD_IMPERSONATE_SA. If neither is configured the MFM
# scan endpoint will return a clear error rather than crashing.
DWD_SERVICE_ACCOUNT_KEY = os.environ.get("DWD_SERVICE_ACCOUNT_KEY", "")
DWD_IMPERSONATE_SA = os.environ.get("DWD_IMPERSONATE_SA", "")

DWD_WORKSPACE_ACCOUNTS = _csv_env(
    "DWD_WORKSPACE_ACCOUNTS",
    "admin@insideraiagency.com,sportsai@insidersportsai.com",
)

DWD_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# ---------- OAuth2 personal Gmail (NEVER include kheprahel@gmail.com) ----------
OAUTH2_CLIENT_SECRETS = os.environ.get("OAUTH2_CLIENT_SECRETS", "")
OAUTH2_TOKEN_DIR = os.environ.get("OAUTH2_TOKEN_DIR", "tokens")

# kheprahel@gmail.com is the owner's personal account; per the naming locks
# in the Genesis directive it MUST NOT be included here.
OAUTH2_GMAIL_ACCOUNTS = _csv_env(
    "OAUTH2_GMAIL_ACCOUNTS",
    "insidersports@gmail.com",
)

OAUTH2_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

GMAIL_SEARCH_QUERY = os.environ.get(
    "GMAIL_SEARCH_QUERY",
    'domain OR purchase OR receipt OR "API key" OR Namecheap OR '
    '"Unstoppable Domains" OR confirmation OR architecture OR invoice',
)

# ---------- Sovereign-Dispatch ingestion behaviour ----------
SOVEREIGN_DISPATCH = {
    "mode": os.environ.get("SD_MODE", "unrestricted"),
    "file_type_filter": None,
    "search_terms": None,
    "folder_filter": None,
    "mime_type_exclusions": [],
    "max_depth": None,
    "page_size_drive": int(os.environ.get("SD_DRIVE_PAGE_SIZE", "1000")),
    "page_size_gmail": int(os.environ.get("SD_GMAIL_PAGE_SIZE", "500")),
}


def assert_personal_account_not_leaked() -> None:
    """Hard check: kheprahel@gmail.com must never appear in agent-accessible lists."""
    forbidden = "kheprahel@gmail.com"
    leaks: list[str] = []
    if forbidden in DWD_WORKSPACE_ACCOUNTS:
        leaks.append("DWD_WORKSPACE_ACCOUNTS")
    if forbidden in OAUTH2_GMAIL_ACCOUNTS:
        leaks.append("OAUTH2_GMAIL_ACCOUNTS")
    if leaks:
        raise RuntimeError(
            f"naming-lock violation: {forbidden} present in {', '.join(leaks)}. "
            "Personal account must never be granted to agents."
        )


# Run the check at import time so misconfigured deploys fail fast.
assert_personal_account_not_leaked()
