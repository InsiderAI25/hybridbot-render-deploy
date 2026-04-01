"""
Monique File Manager - Configuration
Sovereign-dispatch mode: unrestricted full-ingestion scan.
"""

import os

# ---------- Gemini ----------
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")

# ---------- GCP Project ----------
GCP_PROJECT = os.environ.get("GCP_PROJECT", "genesis-ai-prod-7x2k")

# ---------- BigQuery ----------
BQ_DATASET = os.environ.get("BQ_DATASET", "genesis_shared_memory")
BQ_TABLE = os.environ.get("BQ_TABLE", "file_catalog")

# ---------- Pub/Sub ----------
PUBSUB_TOPIC = os.environ.get(
    "PUBSUB_TOPIC",
    f"projects/{GCP_PROJECT}/topics/genesis-file-manager-complete",
)

# ---------- Domain-Wide Delegation (Workspace accounts) ----------
# Path to the service account JSON key used for DWD
DWD_SERVICE_ACCOUNT_KEY = os.environ.get("DWD_SERVICE_ACCOUNT_KEY", "service_account.json")

DWD_WORKSPACE_ACCOUNTS = [
    "admin@insideraiagency.com",
    "sportsai@insidersportsai.com",
]

# Scopes required for full workspace ingestion
DWD_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# ---------- OAuth2 Personal Gmail ----------
OAUTH2_CLIENT_SECRETS = os.environ.get("OAUTH2_CLIENT_SECRETS", "client_secrets.json")
OAUTH2_TOKEN_DIR = os.environ.get("OAUTH2_TOKEN_DIR", "tokens")

OAUTH2_GMAIL_ACCOUNTS = [
    "kheprahel@gmail.com",
    "insidersports@gmail.com",
]

OAUTH2_GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Gmail search query – broad scan for domain purchases, API keys, receipts, etc.
GMAIL_SEARCH_QUERY = (
    "domain OR purchase OR receipt OR API key OR Namecheap OR "
    "\"Unstoppable Domains\" OR confirmation OR architecture OR invoice"
)

# ---------- Sovereign-Dispatch: Unrestricted Mode ----------
# No file-type restrictions, no search-term filters, full recursive scan.
SOVEREIGN_DISPATCH = {
    "mode": "unrestricted",
    "file_type_filter": None,       # None = all types
    "search_terms": None,           # None = no keyword filter
    "folder_filter": None,          # None = all folders, full recursive
    "mime_type_exclusions": [],     # empty = exclude nothing
    "max_depth": None,              # None = unlimited recursion
}
