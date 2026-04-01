"""
Monique File Manager — Sovereign-Dispatch Full-Ingestion Engine

Unrestricted mode: scans ALL files across Google Drive (via DWD) and
personal Gmail (via OAuth2), catalogs everything in BigQuery, then
publishes a completion event to Pub/Sub.
"""

import json
import logging
import os
import time
import datetime
from pathlib import Path

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.cloud import bigquery, pubsub_v1

import config

logger = logging.getLogger("monique")

# ────────────────────────────────────────────────────────────────────
# Auth helpers
# ────────────────────────────────────────────────────────────────────

def _get_dwd_credentials(subject_email: str, scopes: list[str]):
    """Create domain-wide-delegated credentials impersonating *subject_email*."""
    creds = service_account.Credentials.from_service_account_file(
        config.DWD_SERVICE_ACCOUNT_KEY,
        scopes=scopes,
        subject=subject_email,
    )
    return creds


def _get_oauth2_credentials(account_email: str):
    """
    Return OAuth2 credentials for a personal Gmail account.

    Tokens are cached in config.OAUTH2_TOKEN_DIR/<account>.json.
    If no cached token exists the consent flow is triggered (interactive).
    """
    token_dir = Path(config.OAUTH2_TOKEN_DIR)
    token_dir.mkdir(parents=True, exist_ok=True)
    token_path = token_dir / f"{account_email}.json"

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(
            str(token_path), config.OAUTH2_GMAIL_SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.OAUTH2_CLIENT_SECRETS,
                scopes=config.OAUTH2_GMAIL_SCOPES,
            )
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())

    return creds


# ────────────────────────────────────────────────────────────────────
# BigQuery helpers
# ────────────────────────────────────────────────────────────────────

def _ensure_bq_table(bq_client: bigquery.Client):
    """Create the catalog table if it does not exist."""
    dataset_ref = bq_client.dataset(config.BQ_DATASET)
    table_ref = dataset_ref.table(config.BQ_TABLE)

    schema = [
        bigquery.SchemaField("file_id", "STRING"),
        bigquery.SchemaField("file_name", "STRING"),
        bigquery.SchemaField("mime_type", "STRING"),
        bigquery.SchemaField("source_account", "STRING"),
        bigquery.SchemaField("source_type", "STRING"),   # drive | gmail
        bigquery.SchemaField("parent_folder", "STRING"),
        bigquery.SchemaField("web_view_link", "STRING"),
        bigquery.SchemaField("size_bytes", "INTEGER"),
        bigquery.SchemaField("created_time", "TIMESTAMP"),
        bigquery.SchemaField("modified_time", "TIMESTAMP"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP"),
        bigquery.SchemaField("snippet", "STRING"),
        bigquery.SchemaField("labels", "STRING", mode="REPEATED"),
    ]

    table = bigquery.Table(table_ref, schema=schema)
    table = bq_client.create_table(table, exists_ok=True)
    logger.info("BigQuery table ready: %s.%s", config.BQ_DATASET, config.BQ_TABLE)
    return table_ref


def _insert_rows(bq_client: bigquery.Client, table_ref, rows: list[dict]):
    """Stream rows into BigQuery."""
    if not rows:
        return
    errors = bq_client.insert_rows_json(table_ref, rows)
    if errors:
        logger.error("BigQuery insert errors: %s", errors)
    else:
        logger.info("Inserted %d rows into BigQuery", len(rows))


# ────────────────────────────────────────────────────────────────────
# Google Drive — full recursive scan (DWD)
# ────────────────────────────────────────────────────────────────────

def _scan_drive(account_email: str, bq_client: bigquery.Client, table_ref) -> int:
    """
    Full unrestricted scan of a Workspace account's Drive.
    No mime-type filter, no search query, no folder restriction.
    Returns the number of files cataloged.
    """
    creds = _get_dwd_credentials(account_email, config.DWD_SCOPES)
    service = build("drive", "v3", credentials=creds)

    total = 0
    page_token = None
    now = datetime.datetime.utcnow().isoformat() + "Z"

    while True:
        # Unrestricted: no q= filter → returns every file the account can see
        resp = service.files().list(
            corpora="allDrives",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields=(
                "nextPageToken,"
                "files(id,name,mimeType,parents,webViewLink,"
                "size,createdTime,modifiedTime)"
            ),
            pageSize=1000,
            pageToken=page_token,
        ).execute()

        files = resp.get("files", [])
        rows = []
        for f in files:
            rows.append({
                "file_id": f["id"],
                "file_name": f.get("name", ""),
                "mime_type": f.get("mimeType", ""),
                "source_account": account_email,
                "source_type": "drive",
                "parent_folder": (f.get("parents") or [""])[0],
                "web_view_link": f.get("webViewLink", ""),
                "size_bytes": int(f.get("size", 0)),
                "created_time": f.get("createdTime"),
                "modified_time": f.get("modifiedTime"),
                "ingested_at": now,
                "snippet": "",
                "labels": ["sovereign-dispatch", "unrestricted"],
            })

        _insert_rows(bq_client, table_ref, rows)
        total += len(rows)
        logger.info("[Drive:%s] cataloged %d files (total %d)", account_email, len(rows), total)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return total


# ────────────────────────────────────────────────────────────────────
# Gmail — OAuth2 personal account scan
# ────────────────────────────────────────────────────────────────────

def _scan_gmail(account_email: str, bq_client: bigquery.Client, table_ref) -> int:
    """
    Scan a personal Gmail account for domain purchases, API keys,
    receipts, Namecheap/Unstoppable Domains confirmations, and
    early architecture emails.  Returns number of messages cataloged.
    """
    creds = _get_oauth2_credentials(account_email)
    service = build("gmail", "v1", credentials=creds)

    total = 0
    page_token = None
    now = datetime.datetime.utcnow().isoformat() + "Z"

    while True:
        resp = service.users().messages().list(
            userId="me",
            q=config.GMAIL_SEARCH_QUERY,
            pageToken=page_token,
            maxResults=500,
        ).execute()

        messages = resp.get("messages", [])
        rows = []
        for msg_stub in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_stub["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            rows.append({
                "file_id": msg["id"],
                "file_name": headers.get("Subject", "(no subject)"),
                "mime_type": "message/rfc822",
                "source_account": account_email,
                "source_type": "gmail",
                "parent_folder": "",
                "web_view_link": f"https://mail.google.com/mail/#inbox/{msg['id']}",
                "size_bytes": int(msg.get("sizeEstimate", 0)),
                "created_time": headers.get("Date"),
                "modified_time": headers.get("Date"),
                "ingested_at": now,
                "snippet": msg.get("snippet", ""),
                "labels": ["sovereign-dispatch", "gmail-scan", "source:" + account_email],
            })

        _insert_rows(bq_client, table_ref, rows)
        total += len(rows)
        logger.info("[Gmail:%s] cataloged %d messages (total %d)", account_email, len(rows), total)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return total


# ────────────────────────────────────────────────────────────────────
# Pub/Sub completion event
# ────────────────────────────────────────────────────────────────────

def _publish_completion(results: dict):
    """Publish completion event to genesis-file-manager-complete topic."""
    publisher = pubsub_v1.PublisherClient()
    payload = json.dumps({
        "event": "sovereign-dispatch-complete",
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "results": results,
    }).encode("utf-8")

    future = publisher.publish(config.PUBSUB_TOPIC, payload)
    message_id = future.result()
    logger.info("Published completion event, message_id=%s", message_id)
    return message_id


# ────────────────────────────────────────────────────────────────────
# Sovereign-Dispatch Orchestrator
# ────────────────────────────────────────────────────────────────────

def run_sovereign_dispatch() -> dict:
    """
    Execute the full unrestricted ingestion scan:
      1. Drive scan for each Workspace account (DWD)
      2. Gmail scan for each personal account (OAuth2)
      3. Catalog everything in BigQuery genesis_shared_memory
      4. Publish completion to Pub/Sub
    Returns summary dict.
    """
    logger.info("=== Sovereign-Dispatch: UNRESTRICTED FULL INGESTION ===")
    logger.info("Config: %s", json.dumps(config.SOVEREIGN_DISPATCH, default=str))

    bq_client = bigquery.Client(project=config.GCP_PROJECT)
    table_ref = _ensure_bq_table(bq_client)

    results = {
        "drive_accounts": {},
        "gmail_accounts": {},
        "total_files": 0,
        "total_gmail_messages": 0,
    }

    # ── Phase 1: Workspace Drive scans (DWD) ──
    for account in config.DWD_WORKSPACE_ACCOUNTS:
        try:
            count = _scan_drive(account, bq_client, table_ref)
            results["drive_accounts"][account] = {"status": "ok", "files": count}
            results["total_files"] += count
        except Exception:
            logger.exception("Drive scan failed for %s", account)
            results["drive_accounts"][account] = {"status": "error"}

    # ── Phase 2: Personal Gmail scans (OAuth2) ──
    for account in config.OAUTH2_GMAIL_ACCOUNTS:
        try:
            count = _scan_gmail(account, bq_client, table_ref)
            results["gmail_accounts"][account] = {"status": "ok", "messages": count}
            results["total_gmail_messages"] += count
        except Exception:
            logger.exception("Gmail scan failed for %s", account)
            results["gmail_accounts"][account] = {"status": "error"}

    # ── Phase 3: Publish completion ──
    try:
        msg_id = _publish_completion(results)
        results["pubsub_message_id"] = msg_id
    except Exception:
        logger.exception("Failed to publish completion event")
        results["pubsub_message_id"] = None

    logger.info("=== Sovereign-Dispatch complete: %s ===", json.dumps(results, default=str))
    return results
