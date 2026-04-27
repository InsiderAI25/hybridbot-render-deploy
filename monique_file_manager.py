"""
Monique File Manager — Sovereign-Dispatch ingestion engine.

Real logic only:
  * Drive scan via Workspace DWD impersonation, paginated, all drives
  * Gmail scan via OAuth2 (personal accounts) or DWD (workspace accounts)
  * Catalogs every result into BigQuery genesis_shared_memory
  * Publishes a completion event to Pub/Sub
  * Logs the run summary to Genesis Open Brain

Auth:
  * Default identity comes from google.auth.default() (Cloud Run attached SA).
  * For DWD impersonation we try modern impersonated_credentials when
    DWD_IMPERSONATE_SA is set; otherwise fall back to a service-account JSON
    key file (DWD_SERVICE_ACCOUNT_KEY). The OAuth2 personal-account path
    requires a pre-cached refresh token in OAUTH2_TOKEN_DIR (no interactive
    flow in headless deployments).

Errors are logged and surfaced; nothing is silently swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from google.api_core import exceptions as gax_exceptions
from google.api_core import retry as gax_retry
from google.auth import default as google_auth_default
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request as AuthRequest
from google.cloud import bigquery, pubsub_v1
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as OAuth2Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config
import open_brain

logger = logging.getLogger("monique.fm")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ────────────────────────────────────────────────────────────────────
# Auth
# ────────────────────────────────────────────────────────────────────


class DwdConfigError(RuntimeError):
    """DWD requested but neither key file nor impersonation SA configured."""


def _get_dwd_credentials(subject_email: str, scopes: list[str]):
    """Return delegated credentials for *subject_email*.

    Prefers keyless impersonation when DWD_IMPERSONATE_SA is set:
      ADC -> impersonated_credentials.Credentials -> with_subject(user)
    Falls back to service_account.Credentials.from_service_account_file when
    DWD_SERVICE_ACCOUNT_KEY points to a real JSON key.
    """
    if config.DWD_IMPERSONATE_SA:
        source, _ = google_auth_default()
        target = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=config.DWD_IMPERSONATE_SA,
            target_scopes=scopes,
            lifetime=3600,
        )
        # impersonated_credentials supports subject delegation in google-auth >= 2.16
        return target.with_subject(subject_email)

    key_path = config.DWD_SERVICE_ACCOUNT_KEY
    if key_path and Path(key_path).is_file():
        return service_account.Credentials.from_service_account_file(
            key_path, scopes=scopes, subject=subject_email,
        )

    raise DwdConfigError(
        "Neither DWD_IMPERSONATE_SA nor DWD_SERVICE_ACCOUNT_KEY is configured; "
        "cannot impersonate Workspace user for DWD."
    )


def _get_oauth2_credentials(account_email: str) -> OAuth2Credentials:
    """Load cached OAuth2 token for a personal Gmail account.

    Headless: refuses to run an interactive consent flow. The caller is
    expected to have placed the token file at OAUTH2_TOKEN_DIR/<email>.json
    out-of-band.
    """
    token_path = Path(config.OAUTH2_TOKEN_DIR) / f"{account_email}.json"
    if not token_path.is_file():
        raise FileNotFoundError(
            f"OAuth2 token for {account_email} not found at {token_path}; "
            "complete the consent flow offline and copy the token file in."
        )
    creds = OAuth2Credentials.from_authorized_user_file(
        str(token_path), config.OAUTH2_GMAIL_SCOPES,
    )
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise RuntimeError(
                f"OAuth2 token for {account_email} is invalid and not refreshable; "
                "regenerate it offline."
            )
        creds.refresh(AuthRequest())
        token_path.write_text(creds.to_json())
    return creds


# ────────────────────────────────────────────────────────────────────
# BigQuery
# ────────────────────────────────────────────────────────────────────

_FILE_CATALOG_SCHEMA = [
    bigquery.SchemaField("file_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("file_name", "STRING"),
    bigquery.SchemaField("mime_type", "STRING"),
    bigquery.SchemaField("source_account", "STRING"),
    bigquery.SchemaField("source_type", "STRING"),
    bigquery.SchemaField("parent_folder", "STRING"),
    bigquery.SchemaField("web_view_link", "STRING"),
    bigquery.SchemaField("size_bytes", "INTEGER"),
    bigquery.SchemaField("created_time", "TIMESTAMP"),
    bigquery.SchemaField("modified_time", "TIMESTAMP"),
    bigquery.SchemaField("ingested_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("snippet", "STRING"),
    bigquery.SchemaField("labels", "STRING", mode="REPEATED"),
]


def _ensure_bq_table(bq_client: bigquery.Client) -> bigquery.TableReference:
    dataset_ref = bigquery.DatasetReference(bq_client.project, config.BQ_DATASET)
    table_ref = dataset_ref.table(config.BQ_TABLE)
    table = bigquery.Table(table_ref, schema=_FILE_CATALOG_SCHEMA)
    bq_client.create_table(table, exists_ok=True)
    logger.info("BigQuery table ready: %s.%s.%s",
                bq_client.project, config.BQ_DATASET, config.BQ_TABLE)
    return table_ref


def _insert_rows(bq_client: bigquery.Client,
                 table_ref: bigquery.TableReference,
                 rows: list[dict]) -> None:
    if not rows:
        return
    errors = bq_client.insert_rows_json(table_ref, rows)
    if errors:
        logger.error("BigQuery insert errors: %s", errors)
        raise RuntimeError(f"BigQuery insert had {len(errors)} errors")
    logger.info("Inserted %d rows into %s", len(rows), config.BQ_TABLE)


# ────────────────────────────────────────────────────────────────────
# Drive scan (DWD)
# ────────────────────────────────────────────────────────────────────


_drive_retry = gax_retry.Retry(
    initial=1.0, maximum=30.0, multiplier=2.0, deadline=600.0,
    predicate=gax_retry.if_exception_type(
        gax_exceptions.TooManyRequests,
        gax_exceptions.ServiceUnavailable,
        gax_exceptions.InternalServerError,
    ),
)


def _scan_drive(account_email: str,
                bq_client: bigquery.Client,
                table_ref: bigquery.TableReference) -> int:
    creds = _get_dwd_credentials(account_email, config.DWD_SCOPES)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    total = 0
    page_token: str | None = None
    page_size = config.SOVEREIGN_DISPATCH["page_size_drive"]
    fields = (
        "nextPageToken,"
        "files(id,name,mimeType,parents,webViewLink,size,"
        "createdTime,modifiedTime)"
    )

    while True:
        try:
            resp = service.files().list(
                corpora="allDrives",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields=fields,
                pageSize=page_size,
                pageToken=page_token,
            ).execute()
        except HttpError as exc:
            logger.error("Drive list failed for %s: %s", account_email, exc)
            raise

        files = resp.get("files", [])
        now = _now_iso()
        rows = [
            {
                "file_id": f["id"],
                "file_name": f.get("name", ""),
                "mime_type": f.get("mimeType", ""),
                "source_account": account_email,
                "source_type": "drive",
                "parent_folder": (f.get("parents") or [""])[0],
                "web_view_link": f.get("webViewLink", ""),
                "size_bytes": int(f.get("size", 0) or 0),
                "created_time": f.get("createdTime"),
                "modified_time": f.get("modifiedTime"),
                "ingested_at": now,
                "snippet": "",
                "labels": ["sovereign-dispatch", "drive", f"account:{account_email}"],
            }
            for f in files
        ]
        _insert_rows(bq_client, table_ref, rows)
        total += len(rows)
        logger.info("[Drive:%s] +%d (total %d)", account_email, len(rows), total)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return total


# ────────────────────────────────────────────────────────────────────
# Gmail scan (OAuth2 personal)
# ────────────────────────────────────────────────────────────────────


def _scan_gmail(account_email: str,
                bq_client: bigquery.Client,
                table_ref: bigquery.TableReference) -> int:
    creds = _get_oauth2_credentials(account_email)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    total = 0
    page_token: str | None = None
    page_size = config.SOVEREIGN_DISPATCH["page_size_gmail"]

    while True:
        resp = service.users().messages().list(
            userId="me",
            q=config.GMAIL_SEARCH_QUERY,
            pageToken=page_token,
            maxResults=page_size,
        ).execute()

        message_stubs = resp.get("messages", [])
        now = _now_iso()
        rows: list[dict] = []
        for stub in message_stubs:
            try:
                msg = service.users().messages().get(
                    userId="me", id=stub["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
            except HttpError as exc:
                logger.warning("Gmail get failed (%s/%s): %s",
                               account_email, stub["id"], exc)
                continue

            headers = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            rows.append({
                "file_id": msg["id"],
                "file_name": headers.get("Subject", "(no subject)"),
                "mime_type": "message/rfc822",
                "source_account": account_email,
                "source_type": "gmail",
                "parent_folder": "",
                "web_view_link": f"https://mail.google.com/mail/#inbox/{msg['id']}",
                "size_bytes": int(msg.get("sizeEstimate", 0) or 0),
                "created_time": headers.get("Date"),
                "modified_time": headers.get("Date"),
                "ingested_at": now,
                "snippet": msg.get("snippet", ""),
                "labels": ["sovereign-dispatch", "gmail", f"account:{account_email}"],
            })

        _insert_rows(bq_client, table_ref, rows)
        total += len(rows)
        logger.info("[Gmail:%s] +%d (total %d)", account_email, len(rows), total)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return total


# ────────────────────────────────────────────────────────────────────
# Pub/Sub
# ────────────────────────────────────────────────────────────────────


def _publish_completion(results: dict) -> str | None:
    publisher = pubsub_v1.PublisherClient()
    payload = json.dumps({
        "event": "sovereign-dispatch-complete",
        "timestamp": _now_iso(),
        "results": results,
    }).encode("utf-8")
    future = publisher.publish(config.PUBSUB_TOPIC, payload)
    message_id = future.result(timeout=30)
    logger.info("Published completion event id=%s topic=%s",
                message_id, config.PUBSUB_TOPIC)
    return message_id


# ────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────


def run_sovereign_dispatch() -> dict:
    """Execute Drive + Gmail ingestion, BQ catalog, Pub/Sub event, Open Brain log."""
    logger.info("=== Sovereign-Dispatch START === config=%s",
                json.dumps(config.SOVEREIGN_DISPATCH, default=str))

    bq_client = bigquery.Client(project=config.GCP_PROJECT)
    table_ref = _ensure_bq_table(bq_client)

    results: dict = {
        "started_at": _now_iso(),
        "drive_accounts": {},
        "gmail_accounts": {},
        "total_files": 0,
        "total_gmail_messages": 0,
    }

    for account in config.DWD_WORKSPACE_ACCOUNTS:
        try:
            count = _scan_drive(account, bq_client, table_ref)
            results["drive_accounts"][account] = {"status": "ok", "files": count}
            results["total_files"] += count
        except Exception as exc:
            logger.exception("Drive scan failed for %s", account)
            results["drive_accounts"][account] = {"status": "error", "error": str(exc)}

    for account in config.OAUTH2_GMAIL_ACCOUNTS:
        try:
            count = _scan_gmail(account, bq_client, table_ref)
            results["gmail_accounts"][account] = {"status": "ok", "messages": count}
            results["total_gmail_messages"] += count
        except Exception as exc:
            logger.exception("Gmail scan failed for %s", account)
            results["gmail_accounts"][account] = {"status": "error", "error": str(exc)}

    try:
        results["pubsub_message_id"] = _publish_completion(results)
    except Exception as exc:
        logger.exception("Pub/Sub publish failed")
        results["pubsub_message_id"] = None
        results["pubsub_error"] = str(exc)

    results["finished_at"] = _now_iso()

    # Best-effort log to Open Brain. Run synchronously via a fresh event loop
    # because run_sovereign_dispatch is invoked from a worker thread.
    try:
        asyncio.run(open_brain.memory_store(
            agent_name=config.AGENT_NAME,
            event="sovereign_dispatch_complete",
            data={
                "drive_accounts": results["drive_accounts"],
                "gmail_accounts": results["gmail_accounts"],
                "total_files": results["total_files"],
                "total_gmail_messages": results["total_gmail_messages"],
                "pubsub_message_id": results.get("pubsub_message_id"),
            },
        ))
    except Exception:
        logger.exception("Open Brain memory_store failed (non-fatal)")

    logger.info("=== Sovereign-Dispatch END === %s",
                json.dumps(results, default=str))
    return results
