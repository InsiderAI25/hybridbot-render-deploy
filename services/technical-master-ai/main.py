"""
Technical Master AI (TMA) — central audit ledger.

Every agent writes significant actions here so the empire has one provable
log. BQ-backed (`genesis_shared_memory.tma_audit_log`). Reads are gated by
caller allowlist; writes are open to any authenticated empire SA but
recorded with the verified caller email so impersonation is impossible.

Endpoints:
  GET  /health
  GET  /status
  POST /audit/log     — log a single audit row
  POST /audit/bulk    — log many rows in one shot
  GET  /audit/query   — search audit history
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
logger = logging.getLogger("tma")

SERVICE_VERSION = os.environ.get("K_REVISION", "dev")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "genesis-ai-prod-7x2k")
BQ_DATASET = os.environ.get("BQ_DATASET", "genesis_shared_memory")
BQ_AUDIT_TABLE = os.environ.get("BQ_AUDIT_TABLE", "tma_audit_log")

# Read-side allowlist. Writes are accepted from any authenticated agent
# whose SA email belongs to one of AUDIT_WRITE_PROJECTS *or* is the default
# Compute Engine SA for one of AUDIT_WRITE_PROJECT_NUMBERS.
AUDIT_READ_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("AUDIT_READ_AGENTS", "").split(",")
    if a.strip()
}
AUDIT_WRITE_PROJECTS = {
    p.strip()
    for p in os.environ.get("AUDIT_WRITE_PROJECTS", "").split(",")
    if p.strip()
}
# Map of "<project-number>" -> "<project-id>". Default-compute SAs use the
# project-number form (`<num>-compute@developer.gserviceaccount.com`), which
# bypasses the user-managed-SA suffix check unless we know the numbers too.
AUDIT_WRITE_PROJECT_NUMBERS = {
    n.strip()
    for n in os.environ.get("AUDIT_WRITE_PROJECT_NUMBERS", "").split(",")
    if n.strip().isdigit()
}

# Admin endpoints (project / service / IAM audits) need elevated read scope.
ADMIN_AGENTS = {
    a.strip().lower()
    for a in os.environ.get("ADMIN_AGENTS", "").split(",")
    if a.strip()
}
# Projects TMA is allowed to inspect via Resource Manager / Cloud Run admin.
# When unset, admin endpoints return 503.
ADMIN_PROJECTS = [
    p.strip()
    for p in os.environ.get("ADMIN_PROJECTS", "").split(",")
    if p.strip()
]

# How long since last revision before a service is considered "stale".
STALE_AFTER_DAYS = int(os.environ.get("STALE_AFTER_DAYS", "60"))


_AUDIT_SCHEMA = [
    SchemaField("audit_id", "STRING", mode="REQUIRED"),
    SchemaField("agent", "STRING", mode="REQUIRED"),
    SchemaField("event", "STRING", mode="REQUIRED"),
    SchemaField("severity", "STRING"),         # info | warn | error | critical
    SchemaField("data_json", "STRING"),
    SchemaField("caller_email", "STRING", mode="REQUIRED"),
    SchemaField("logged_at", "TIMESTAMP", mode="REQUIRED"),
]


app = FastAPI(title="Technical Master AI", version=SERVICE_VERSION)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _bq() -> bigquery.Client:
    return bigquery.Client(project=GCP_PROJECT)


def _ensure_table(client: bigquery.Client) -> bigquery.TableReference:
    ref = bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(BQ_AUDIT_TABLE)
    table = bigquery.Table(ref, schema=_AUDIT_SCHEMA)
    table.time_partitioning = bigquery.TimePartitioning(field="logged_at")
    client.create_table(table, exists_ok=True)
    return ref


def _is_writer_email(email: str) -> bool:
    """Accept user-managed and default-compute SAs from allowed projects."""
    if not email:
        return False
    # User-managed SA: <name>@<project-id>.iam.gserviceaccount.com
    for project in AUDIT_WRITE_PROJECTS:
        if email.endswith(f"@{project}.iam.gserviceaccount.com"):
            return True
    # Default Compute Engine SA: <project-number>-compute@developer.gserviceaccount.com
    if email.endswith("@developer.gserviceaccount.com"):
        local = email.split("@", 1)[0]
        if local.endswith("-compute"):
            number = local[: -len("-compute")]
            if number in AUDIT_WRITE_PROJECT_NUMBERS:
                return True
    return False


async def _require_writer(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if not AUDIT_WRITE_PROJECTS and not AUDIT_WRITE_PROJECT_NUMBERS:
        raise HTTPException(
            status_code=503,
            detail="AUDIT_WRITE_PROJECTS / AUDIT_WRITE_PROJECT_NUMBERS not configured; refusing writes",
        )
    if _is_writer_email(email):
        return email
    raise HTTPException(status_code=403, detail=f"writer not allowed: {email}")


async def _require_reader(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if email not in AUDIT_READ_AGENTS:
        raise HTTPException(status_code=403, detail=f"reader not allowlisted: {email}")
    return email


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "technical-master-ai",
            "revision": SERVICE_VERSION, "timestamp": _now_iso()}


@app.get("/status")
def status_endpoint() -> dict[str, Any]:
    return {
        "service": "technical-master-ai",
        "revision": SERVICE_VERSION,
        "audit_table": f"{GCP_PROJECT}.{BQ_DATASET}.{BQ_AUDIT_TABLE}",
        "writers_projects": sorted(AUDIT_WRITE_PROJECTS),
        "readers": len(AUDIT_READ_AGENTS),
        "timestamp": _now_iso(),
    }


def _row_for(payload: dict[str, Any], caller: str) -> dict[str, Any]:
    import uuid
    agent = (payload.get("agent") or "").strip()
    event = (payload.get("event") or "").strip()
    if not agent or not event:
        raise HTTPException(status_code=400, detail="agent and event are required")
    severity = (payload.get("severity") or "info").lower()
    if severity not in {"info", "warn", "error", "critical"}:
        raise HTTPException(status_code=400, detail="severity must be info|warn|error|critical")
    return {
        "audit_id": uuid.uuid4().hex,
        "agent": agent,
        "event": event,
        "severity": severity,
        "data_json": json.dumps(payload.get("data") or {}, default=str),
        "caller_email": caller,
        "logged_at": _now_iso(),
    }


@app.post("/audit/log")
async def audit_log(request: Request,
                    caller: str = Depends(_require_writer)) -> dict[str, Any]:
    body = await request.json()
    row = _row_for(body, caller)
    client = _bq()
    table_ref = _ensure_table(client)
    errors = client.insert_rows_json(table_ref, [row])
    if errors:
        raise HTTPException(status_code=500, detail=f"BQ insert: {errors}")
    return {"audit_id": row["audit_id"], "logged_at": row["logged_at"]}


@app.post("/audit/bulk")
async def audit_bulk(request: Request,
                     caller: str = Depends(_require_writer)) -> dict[str, Any]:
    body = await request.json()
    items = body.get("rows") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="rows[] is required")
    if len(items) > 500:
        raise HTTPException(status_code=413, detail="max 500 rows per call")
    rows = [_row_for(item, caller) for item in items]
    client = _bq()
    table_ref = _ensure_table(client)
    errors = client.insert_rows_json(table_ref, rows)
    if errors:
        raise HTTPException(status_code=500, detail=f"BQ insert: {errors}")
    return {"inserted": len(rows)}


@app.get("/audit/query")
def audit_query(agent: str | None = Query(default=None),
                event: str | None = Query(default=None),
                severity: str | None = Query(default=None),
                limit: int = Query(100, ge=1, le=1000),
                caller: str = Depends(_require_reader)) -> dict[str, Any]:
    client = _bq()
    _ensure_table(client)
    where = []
    params: list[bigquery.ScalarQueryParameter] = [
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]
    if agent:
        where.append("agent = @agent")
        params.append(bigquery.ScalarQueryParameter("agent", "STRING", agent))
    if event:
        where.append("event = @event")
        params.append(bigquery.ScalarQueryParameter("event", "STRING", event))
    if severity:
        where.append("severity = @severity")
        params.append(bigquery.ScalarQueryParameter("severity", "STRING", severity))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = list(client.query(
        f"""SELECT audit_id, agent, event, severity, data_json,
                   caller_email, logged_at
            FROM `{GCP_PROJECT}.{BQ_DATASET}.{BQ_AUDIT_TABLE}`
            {where_sql}
            ORDER BY logged_at DESC LIMIT @limit""",
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result())

    return {
        "filters": {"agent": agent, "event": event, "severity": severity},
        "count": len(rows),
        "rows": [
            {
                "audit_id": r.audit_id,
                "agent": r.agent,
                "event": r.event,
                "severity": r.severity,
                "data": json.loads(r.data_json) if r.data_json else {},
                "caller_email": r.caller_email,
                "logged_at": r.logged_at.isoformat(),
            }
            for r in rows
        ],
    }


# ─────────────────────── Admin / cleanup endpoints ──────────────────
# TMA's job per the directive: identify unused projects/services and
# audit service-account hygiene. Read-only — recommendations are returned;
# nothing is mutated. Mutations (delete project / remove SA) are explicitly
# left to a human or a higher-authority workflow gated by Monique CEO.


async def _require_admin(authorization: str | None = Header(default=None)) -> str:
    try:
        claims = auth_helpers.verify_caller_id_token(authorization)
    except auth_helpers.IdentityTokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    email = (auth_helpers.caller_email(claims) or "").lower()
    if not ADMIN_AGENTS:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_AGENTS not configured; admin endpoints disabled",
        )
    if email not in ADMIN_AGENTS:
        raise HTTPException(status_code=403, detail=f"admin not allowlisted: {email}")
    return email


def _ensure_admin_projects() -> list[str]:
    if not ADMIN_PROJECTS:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PROJECTS not configured; nothing to inspect",
        )
    return ADMIN_PROJECTS


def _scan_projects() -> dict[str, Any]:
    """Pure implementation: returns Cloud Run + IAM summary per project."""
    from datetime import timedelta
    from google.cloud import resourcemanager_v3, run_v2

    rm = resourcemanager_v3.ProjectsClient()
    run_admin = run_v2.ServicesClient()

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=STALE_AFTER_DAYS)
    summary: list[dict[str, Any]] = []
    for project_id in _ensure_admin_projects():
        entry: dict[str, Any] = {"project_id": project_id}
        try:
            project = rm.get_project(name=f"projects/{project_id}")
            entry["display_name"] = project.display_name
            entry["state"] = project.state.name
            entry["project_number"] = project.name.split("/", 1)[1]
        except Exception as exc:
            entry["error"] = f"resource manager: {exc}"
            summary.append(entry)
            continue

        services: list[dict[str, Any]] = []
        try:
            parent = f"projects/{project_id}/locations/us-central1"
            for svc in run_admin.list_services(parent=parent):
                update_time = svc.update_time
                if update_time and update_time.tzinfo is None:
                    update_dt = update_time.replace(tzinfo=timezone.utc)
                else:
                    update_dt = update_time
                services.append({
                    "name": svc.name.rsplit("/", 1)[-1],
                    "uri": getattr(svc, "uri", ""),
                    "service_account": svc.template.service_account or "(default-compute)",
                    "last_updated": update_dt.isoformat() if update_dt else None,
                    "last_modifier": getattr(svc, "last_modifier", "") or "",
                    "stale": bool(update_dt and update_dt < cutoff),
                })
        except Exception as exc:
            entry["run_error"] = f"cloud run: {exc}"

        entry["services"] = services
        entry["service_count"] = len(services)
        entry["stale_count"] = sum(1 for s in services if s.get("stale"))
        entry["empty"] = len(services) == 0
        summary.append(entry)

    cleanup = [
        s["project_id"] for s in summary
        if s.get("empty") or (s.get("service_count") and
                              s.get("stale_count") == s.get("service_count"))
    ]
    return {
        "stale_after_days": STALE_AFTER_DAYS,
        "projects": summary,
        "cleanup_candidates": cleanup,
        "timestamp": _now_iso(),
    }


def _scan_services_flat() -> dict[str, Any]:
    from google.cloud import run_v2

    run_admin = run_v2.ServicesClient()
    rows: list[dict[str, Any]] = []
    for project_id in _ensure_admin_projects():
        parent = f"projects/{project_id}/locations/us-central1"
        try:
            for svc in run_admin.list_services(parent=parent):
                rows.append({
                    "project_id": project_id,
                    "service": svc.name.rsplit("/", 1)[-1],
                    "uri": getattr(svc, "uri", ""),
                    "service_account": svc.template.service_account or "(default-compute)",
                    "last_updated": svc.update_time.isoformat() if svc.update_time else None,
                })
        except Exception as exc:
            rows.append({"project_id": project_id, "error": str(exc)})
    return {"services": rows, "timestamp": _now_iso()}


def _scan_service_accounts() -> dict[str, Any]:
    from google.cloud import iam_admin_v1, run_v2

    iam = iam_admin_v1.IAMClient()
    run_admin = run_v2.ServicesClient()

    out: list[dict[str, Any]] = []
    for project_id in _ensure_admin_projects():
        entry: dict[str, Any] = {"project_id": project_id}
        try:
            sas = list(iam.list_service_accounts(name=f"projects/{project_id}"))
            sa_emails = {sa.email.lower() for sa in sas}
            entry["service_accounts"] = [
                {"email": sa.email, "display_name": sa.display_name,
                 "disabled": sa.disabled}
                for sa in sas
            ]
        except Exception as exc:
            entry["error"] = f"iam list: {exc}"
            out.append(entry)
            continue

        anomalies: list[dict[str, Any]] = []
        try:
            for svc in run_admin.list_services(
                parent=f"projects/{project_id}/locations/us-central1"
            ):
                runtime_sa = (svc.template.service_account or "").lower()
                svc_name = svc.name.rsplit("/", 1)[-1]
                if not runtime_sa:
                    anomalies.append({
                        "kind": "default_compute_sa", "service": svc_name,
                        "detail": "uses default compute SA — assign a dedicated SA",
                    })
                elif runtime_sa.endswith("@developer.gserviceaccount.com"):
                    anomalies.append({
                        "kind": "default_compute_sa", "service": svc_name,
                        "service_account": runtime_sa,
                    })
                elif runtime_sa.endswith(f"@{project_id}.iam.gserviceaccount.com"):
                    if runtime_sa not in sa_emails:
                        anomalies.append({
                            "kind": "orphaned_sa_reference", "service": svc_name,
                            "service_account": runtime_sa,
                            "detail": "service references a SA that no longer exists",
                        })
                else:
                    anomalies.append({
                        "kind": "cross_project_sa", "service": svc_name,
                        "service_account": runtime_sa,
                        "detail": "SA email project segment doesn't match host project",
                    })
        except Exception as exc:
            anomalies.append({"kind": "run_list_error", "error": str(exc)})

        entry["anomalies"] = anomalies
        entry["anomaly_count"] = len(anomalies)
        out.append(entry)
    return {"projects": out, "timestamp": _now_iso()}


def _scan_iam_bindings() -> dict[str, Any]:
    from google.cloud import run_v2

    run_admin = run_v2.ServicesClient()
    rows: list[dict[str, Any]] = []
    for project_id in _ensure_admin_projects():
        try:
            for svc in run_admin.list_services(
                parent=f"projects/{project_id}/locations/us-central1"
            ):
                policy = run_admin.get_iam_policy(resource=svc.name)
                invokers: list[str] = []
                for binding in policy.bindings:
                    if binding.role == "roles/run.invoker":
                        invokers.extend(list(binding.members))
                rows.append({
                    "project_id": project_id,
                    "service": svc.name.rsplit("/", 1)[-1],
                    "uri": getattr(svc, "uri", ""),
                    "invokers": invokers,
                    "is_public": "allUsers" in invokers
                                 or "allAuthenticatedUsers" in invokers,
                })
        except Exception as exc:
            rows.append({"project_id": project_id, "error": str(exc)})
    return {"services": rows, "timestamp": _now_iso()}


@app.get("/admin/projects")
def admin_projects_route(caller: str = Depends(_require_admin)) -> dict[str, Any]:
    """Per-project Cloud Run summary; flags empty / fully-stale projects."""
    return _scan_projects()


@app.get("/admin/services")
def admin_services_route(caller: str = Depends(_require_admin)) -> dict[str, Any]:
    """Flat list of every Cloud Run service across ADMIN_PROJECTS."""
    return _scan_services_flat()


@app.get("/admin/service-accounts")
def admin_sa_route(caller: str = Depends(_require_admin)) -> dict[str, Any]:
    """Per-project SA inventory + anomalies (default-compute, orphaned, cross-project)."""
    return _scan_service_accounts()


@app.get("/admin/iam-bindings")
def admin_iam_route(caller: str = Depends(_require_admin)) -> dict[str, Any]:
    """For every Cloud Run service, who has roles/run.invoker."""
    return _scan_iam_bindings()


@app.get("/admin/recommend-cleanup")
def admin_recommend_cleanup(caller: str = Depends(_require_admin)) -> dict[str, Any]:
    """Synthesis: prioritized cleanup recommendations from project + SA scans."""
    projects_info = _scan_projects()
    sa_info = _scan_service_accounts()
    recs: list[dict[str, Any]] = []

    for proj in projects_info["projects"]:
        if proj.get("error"):
            recs.append({
                "severity": "warn", "project_id": proj["project_id"],
                "action": "investigate", "reason": proj["error"],
            })
            continue
        if proj.get("empty"):
            recs.append({
                "severity": "info", "project_id": proj["project_id"],
                "action": "delete_or_repurpose",
                "reason": "no Cloud Run services in us-central1; project appears unused",
            })
        elif proj.get("stale_count") and proj.get("stale_count") == proj.get("service_count"):
            recs.append({
                "severity": "warn", "project_id": proj["project_id"],
                "action": "review_stale_services",
                "reason": (f"all {proj['service_count']} services stale "
                           f"(no deploys in >{STALE_AFTER_DAYS}d)"),
                "services": [s["name"] for s in proj["services"] if s.get("stale")],
            })

    action_for_kind = {
        "default_compute_sa": "create_dedicated_sa_and_redeploy",
        "orphaned_sa_reference": "fix_or_recreate_service_account",
        "cross_project_sa": "verify_cross_project_iam_binding",
        "run_list_error": "grant_tma_run_viewer_on_project",
    }
    for proj in sa_info["projects"]:
        for anomaly in proj.get("anomalies", []):
            kind = anomaly["kind"]
            recs.append({
                "severity": "warn" if kind == "default_compute_sa" else "error",
                "project_id": proj["project_id"],
                "service": anomaly.get("service"),
                "action": action_for_kind.get(kind, "investigate"),
                "reason": anomaly.get("detail") or kind,
                "service_account": anomaly.get("service_account"),
            })

    return {
        "stale_after_days": STALE_AFTER_DAYS,
        "recommendations": recs,
        "summary": {
            "total": len(recs),
            "by_severity": {
                level: sum(1 for r in recs if r["severity"] == level)
                for level in ("info", "warn", "error")
            },
        },
        "timestamp": _now_iso(),
    }


# ──────────────────── Privileged: bootstrap a project ───────────────
# This endpoint enables APIs, creates a runtime SA, and grants a curated
# set of empire IAM bindings on a target project. It is read+WRITE — a
# real ops endpoint for getting genesis-ai-staging / dev / sandbox /
# domain-control online without 17 manual gcloud invocations.
#
# Safety:
#   * caller must be in ADMIN_AGENTS (verified ID token)
#   * caller must include {"confirm":"I_ACKNOWLEDGE"} in the body so a
#     curl-typo can't trigger IAM mutations
#   * every step is audited to tma_audit_log with severity=warn
#   * all operations are idempotent (already-enabled APIs / existing SAs
#     are no-op, returning {step: "<name>", status: "already-present"})


# Standard APIs every empire project needs to be useful.
_STANDARD_APIS = [
    "run.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "secretmanager.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "bigquery.googleapis.com",
    "pubsub.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudkms.googleapis.com",
]
# Optional, opt-in per request. domain-control needs DNS, billing-survival
# needs billing reads, etc.
_OPTIONAL_APIS = {
    "dns": "dns.googleapis.com",
    "billing": "cloudbilling.googleapis.com",
    "drive": "drive.googleapis.com",
    "gmail": "gmail.googleapis.com",
    "kms": "cloudkms.googleapis.com",
}


def _enable_apis(project_id: str, apis: list[str]) -> list[dict[str, Any]]:
    from google.cloud import service_usage_v1

    client = service_usage_v1.ServiceUsageClient()
    parent = f"projects/{project_id}"
    results: list[dict[str, Any]] = []
    for api in apis:
        name = f"{parent}/services/{api}"
        try:
            current = client.get_service(name=name)
            if current.state.name == "ENABLED":
                results.append({"api": api, "status": "already-enabled"})
                continue
            op = client.enable_service(name=name)
            op.result(timeout=120)
            results.append({"api": api, "status": "enabled"})
        except Exception as exc:
            results.append({"api": api, "status": "error", "error": str(exc)})
    return results


def _ensure_runtime_sa(project_id: str, sa_account_id: str,
                       display_name: str) -> dict[str, Any]:
    from google.cloud import iam_admin_v1
    from google.cloud.iam_admin_v1 import types as iam_types

    client = iam_admin_v1.IAMClient()
    expected_email = f"{sa_account_id}@{project_id}.iam.gserviceaccount.com"
    full_name = f"projects/{project_id}/serviceAccounts/{expected_email}"
    try:
        existing = client.get_service_account(name=full_name)
        return {"status": "already-present", "email": existing.email}
    except Exception:
        pass

    try:
        request = iam_types.CreateServiceAccountRequest(
            name=f"projects/{project_id}",
            account_id=sa_account_id,
            service_account=iam_types.ServiceAccount(display_name=display_name),
        )
        sa = client.create_service_account(request=request)
        return {"status": "created", "email": sa.email}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _grant_project_role(project_id: str, member: str, role: str) -> dict[str, Any]:
    """Idempotent IAM-policy add at the project level."""
    from google.cloud import resourcemanager_v3
    from google.iam.v1 import iam_policy_pb2, policy_pb2

    client = resourcemanager_v3.ProjectsClient()
    resource = f"projects/{project_id}"
    try:
        policy = client.get_iam_policy(
            request=iam_policy_pb2.GetIamPolicyRequest(resource=resource)
        )
    except Exception as exc:
        return {"role": role, "member": member, "status": "error",
                "error": f"get_iam_policy: {exc}"}

    for binding in policy.bindings:
        if binding.role == role and member in binding.members:
            return {"role": role, "member": member, "status": "already-bound"}

    new_binding = policy_pb2.Binding(role=role, members=[member])
    policy.bindings.append(new_binding)
    try:
        client.set_iam_policy(
            request=iam_policy_pb2.SetIamPolicyRequest(resource=resource, policy=policy)
        )
        return {"role": role, "member": member, "status": "granted"}
    except Exception as exc:
        return {"role": role, "member": member, "status": "error",
                "error": f"set_iam_policy: {exc}"}


@app.post("/admin/bootstrap-project")
async def admin_bootstrap_project(request: Request,
                                  caller: str = Depends(_require_admin)) -> dict[str, Any]:
    """Bring a fresh project online for empire use.

    Body:
      {
        "project_id":   "genesis-ai-staging",
        "runtime_sa":   "staging-runtime",          # local part only
        "display_name": "Genesis Staging Runtime",
        "extra_apis":   ["dns", "billing"],         # optional
        "extra_bindings": [                         # optional
          {"member": "serviceAccount:open-brain@genesis-ai-prod-7x2k.iam.gserviceaccount.com",
           "role": "roles/run.invoker"}
        ],
        "confirm": "I_ACKNOWLEDGE"
      }

    Returns per-step status. Idempotent.
    """
    body = await request.json()
    project_id = (body.get("project_id") or "").strip()
    runtime_sa_id = (body.get("runtime_sa") or "").strip()
    display_name = (body.get("display_name") or "").strip() or "Empire runtime"
    extra_apis_keys = body.get("extra_apis") or []
    extra_bindings = body.get("extra_bindings") or []

    if body.get("confirm") != "I_ACKNOWLEDGE":
        raise HTTPException(
            status_code=400,
            detail='body must include {"confirm": "I_ACKNOWLEDGE"} for any mutation',
        )
    if not project_id or not runtime_sa_id:
        raise HTTPException(status_code=400, detail="project_id and runtime_sa required")

    # API list = standard + opt-ins
    extra_apis = []
    for key in extra_apis_keys:
        if isinstance(key, str) and key in _OPTIONAL_APIS:
            extra_apis.append(_OPTIONAL_APIS[key])
    apis_to_enable = list(dict.fromkeys(_STANDARD_APIS + extra_apis))

    # 1. Enable APIs
    api_results = _enable_apis(project_id, apis_to_enable)

    # 2. Create runtime SA
    sa_result = _ensure_runtime_sa(project_id, runtime_sa_id, display_name)
    runtime_email = sa_result.get("email") or (
        f"{runtime_sa_id}@{project_id}.iam.gserviceaccount.com"
    )

    # 3. Grant project-level bindings
    base_bindings = [
        # Runtime SA can read its own project (resource manager etc.)
        {"member": f"serviceAccount:{runtime_email}",
         "role": "roles/logging.logWriter"},
        {"member": f"serviceAccount:{runtime_email}",
         "role": "roles/monitoring.metricWriter"},
        {"member": f"serviceAccount:{runtime_email}",
         "role": "roles/cloudtrace.agent"},
    ]
    binding_results: list[dict[str, Any]] = []
    for b in base_bindings + list(extra_bindings):
        member = b.get("member")
        role = b.get("role")
        if not member or not role:
            binding_results.append({"status": "error", "error": "member+role required",
                                    "binding": b})
            continue
        binding_results.append(_grant_project_role(project_id, member, role))

    # 4. Audit
    audit_row = {
        "audit_id": __import__("uuid").uuid4().hex,
        "agent": "technical-master-ai",
        "event": "bootstrap_project",
        "severity": "warn",
        "data_json": json.dumps({
            "project_id": project_id,
            "runtime_email": runtime_email,
            "api_results": api_results,
            "binding_results": binding_results,
        }, default=str),
        "caller_email": caller,
        "logged_at": _now_iso(),
    }
    try:
        client = _bq()
        _ensure_table(client)
        client.insert_rows_json(
            bigquery.DatasetReference(GCP_PROJECT, BQ_DATASET).table(BQ_AUDIT_TABLE),
            [audit_row],
        )
    except Exception as exc:
        logger.warning("bootstrap audit write failed: %s", exc)

    return {
        "project_id": project_id,
        "runtime_email": runtime_email,
        "apis": api_results,
        "service_account": sa_result,
        "bindings": binding_results,
        "audit_id": audit_row["audit_id"],
        "timestamp": _now_iso(),
    }
