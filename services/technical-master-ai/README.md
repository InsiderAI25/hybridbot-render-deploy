# Technical Master AI (TMA)

Central audit ledger **and** the empire's project/service/IAM auditor. Per the
"get TMA on his job" directive, this service owns the answer to "which
projects and services are unused" and "which agent SAs are misconfigured."

## Endpoints

### Audit ledger

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Audit table + writer/reader allowlist sizes |
| POST | `/audit/log` | Single-row write (`{agent, event, severity, data}`) |
| POST | `/audit/bulk` | Up to 500 rows in one call |
| GET | `/audit/query?agent=&event=&severity=&limit=` | Read recent audit rows |

### Admin / cleanup (read-only)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/admin/projects` | Per-project Cloud Run summary; flags empty / fully-stale projects |
| GET | `/admin/services` | Flat list of every Cloud Run service in `us-central1` across `ADMIN_PROJECTS` |
| GET | `/admin/service-accounts` | SA inventory + anomalies (default-compute usage, orphaned SA references, cross-project SAs) |
| GET | `/admin/iam-bindings` | For each Cloud Run service, who has `roles/run.invoker` |
| GET | `/admin/recommend-cleanup` | Synthesis: prioritized cleanup recommendations |

All `/admin/*` endpoints are read-only. Mutations (deleting projects, removing
SAs, granting IAM bindings) are **not** automated — TMA reports; humans or a
governance-gated workflow act.

## Auth model

| Endpoint group | Inbound auth |
|----------------|--------------|
| `/audit/log`, `/audit/bulk` | Any SA whose email matches `AUDIT_WRITE_PROJECTS` (`@<project>.iam.gserviceaccount.com`) **or** `AUDIT_WRITE_PROJECT_NUMBERS` (default-compute `<num>-compute@developer.gserviceaccount.com`) |
| `/audit/query` | SA email in `AUDIT_READ_AGENTS` |
| `/admin/*` | SA email in `ADMIN_AGENTS` |

The default-compute SA path was added to fix a real bug — agents that didn't
override their runtime SA (i.e. were running as the default Compute Engine
SA) were getting 403s on every `/audit/log` because the original allowlist
only matched the user-managed SA email pattern.

## Deploy

```bash
gcloud run deploy technical-master-ai \
  --source=services/technical-master-ai \
  --region=us-central1 \
  --no-allow-unauthenticated \
  --service-account=tma-runtime@PROJECT.iam.gserviceaccount.com \
  --set-env-vars="\
GCP_PROJECT=genesis-ai-prod-7x2k,\
BQ_DATASET=genesis_shared_memory,\
BQ_AUDIT_TABLE=tma_audit_log,\
AUDIT_WRITE_PROJECTS=genesis-ai-prod-7x2k\,insider-core-project,\
AUDIT_WRITE_PROJECT_NUMBERS=865073155062\,663026475135,\
AUDIT_READ_AGENTS=coordinator-v2@...\,monique-controller@...\,monique-ceo@...,\
ADMIN_AGENTS=monique-ceo@...\,monique-controller@...,\
ADMIN_PROJECTS=genesis-ai-prod-7x2k\,insider-core-project\,project-3\,project-4\,project-5\,project-6\,project-7,\
STALE_AFTER_DAYS=60"
```

`AUDIT_WRITE_PROJECT_NUMBERS` accepts comma-separated GCP **project numbers**
(not IDs) for the default-compute SA case. Find them with
`gcloud projects describe <project-id> --format='value(projectNumber)'`.

## Required IAM on the runtime SA

| Role | Granted on | Why |
|------|-----------|-----|
| `roles/bigquery.dataEditor` | the BQ project | Insert into `tma_audit_log` |
| `roles/bigquery.jobUser` | the BQ project | BQ inserts / queries |
| `roles/resourcemanager.projectViewer` | each project in `ADMIN_PROJECTS` | Read project metadata |
| `roles/run.viewer` | each project in `ADMIN_PROJECTS` | List Cloud Run services |
| `roles/iam.serviceAccountViewer` | each project in `ADMIN_PROJECTS` | List SAs to find orphans |

**Granting only viewer roles** keeps TMA strictly read-only. If you later want
TMA to grant `roles/run.invoker` on behalf of agents, that's a separate
endpoint (not yet implemented) — it should be gated by a Monique CEO
governance JWT.

## Sample workflow: find the 4 unused projects

```bash
TMA_URL=https://technical-master-ai-XXXX-uc.a.run.app
TOKEN=$(gcloud auth print-identity-token --audiences="$TMA_URL")

curl -s -H "Authorization: Bearer $TOKEN" "$TMA_URL/admin/recommend-cleanup" | jq '
  .recommendations
  | map(select(.action == "delete_or_repurpose" or .action == "review_stale_services"))
'
```

The output lists each project + the reason TMA flagged it. Use that to
decide which 4 to retire.

## Sample workflow: find SA-email problems in genesis-ai-prod

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$TMA_URL/admin/service-accounts" | jq '
  .projects[] | select(.project_id == "genesis-ai-prod-7x2k") | .anomalies
'
```

Anomaly kinds:
- `default_compute_sa` — service runs as default compute SA. Replace with a dedicated SA so `/audit/log` writes attribute correctly and IAM bindings can be granular.
- `orphaned_sa_reference` — service references a SA that has been deleted. Recreate the SA or redeploy with a different one.
- `cross_project_sa` — service uses an SA from another project. Sometimes intentional (shared agent runtime), sometimes a copy/paste mistake. Verify the cross-project IAM binding exists.
