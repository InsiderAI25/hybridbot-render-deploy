# CLAUDE.md

## Project Overview

**HybridBot / Monique File Manager (MFM)** — FastAPI service that fronts the
Genesis AI Empire's file-ingestion and agent-sync pipeline. Runs on Cloud Run
(`us-central1`). Monique CEO holds 51% voting authority within the empire;
this service is a worker that obeys Coordinator V2 routing and writes truth
to BigQuery via Genesis Open Brain.

> Repository name `hybridbot-render-deploy` is historical. The Render path
> was retired; everything deploys to Cloud Run now. Custom domains live on
> Namecheap and Unstoppable Domains and are mapped to Cloud Run via DNS
> (traditional .com) or Web3 gateways (Unstoppable Web3 TLDs). See
> `DEPLOYMENT.md` for both paths.

**Owner:** InsiderAI25 (sportsai@insidersportsai.com)
**Legal entity:** Insider AI Agency Holdings LLC

## Tech Stack

- **Language:** Python 3.12
- **Framework:** FastAPI
- **Server:** Uvicorn (Gunicorn is **permanently banned** per architecture rules)
- **Auth:** Google ID tokens minted via `google.auth.default()` for cross-Cloud-Run calls
- **Storage:** BigQuery `genesis-ai-prod-7x2k.genesis_shared_memory.*`
- **Eventing:** Pub/Sub `genesis-file-manager-complete`
- **Memory:** Genesis Open Brain (`/memory/retrieve`, `/memory/store`)
- **AI model:** `gemini-3.1-pro-preview` with `VERTEX_AI_LOCATION=global`
- **Region:** `us-central1` only

## Repository Structure

```
hybridbot-render-deploy/        # repo name is historical — Cloud Run only
├── CLAUDE.md                   # This file
├── AUDIT_REPORT.md             # Stub-elimination audit + confidence flags
├── DEPLOYMENT.md               # gcloud run + custom-domain mapping
├── Dockerfile                  # Cloud Run image (FastAPI + Uvicorn)
├── .dockerignore
├── .gitignore
├── auth.py                     # Google ID token helpers (service-to-service)
├── config.py                   # Env-driven config + naming-lock guard
├── open_brain.py               # /memory/retrieve + /memory/store client
├── monique_file_manager.py     # Sovereign-Dispatch ingestion engine
├── main.py                     # FastAPI app entry (Cloud Run)
└── requirements.txt            # Pinned deps (no flask, no gunicorn)
```

## Endpoints

| Method | Path             | Purpose                                           |
|--------|------------------|---------------------------------------------------|
| GET    | `/`              | Banner                                            |
| GET    | `/health`        | Liveness probe + scan state                       |
| GET    | `/status`        | Config snapshot + Open Brain / Newton reachability|
| POST   | `/sync-agent`    | VIE-gated agent sync (Open Brain + Newton)        |
| GET    | `/empire-status` | Sweep `AGENT_REGISTRY` for `/health`              |
| POST   | `/scan`          | Trigger Sovereign-Dispatch ingestion (background) |
| GET    | `/scan/status`   | Current ingestion state                           |

## Naming Locks (NEVER violate)

| Identity                          | Agent-accessible?                |
|-----------------------------------|----------------------------------|
| `kheprahel@gmail.com`             | **No — personal, owner only**    |
| `admin@insideraiagency.com`       | Yes (Workspace, DWD)             |
| `sportsai@insidersportsai.com`    | Yes (Workspace, DWD)             |
| `insidersports@gmail.com`         | Yes (OAuth2 personal Gmail scan) |

`config.assert_personal_account_not_leaked()` runs at import time and refuses
to start the process if `kheprahel@gmail.com` ever lands in
`DWD_WORKSPACE_ACCOUNTS` or `OAUTH2_GMAIL_ACCOUNTS`.

## Architecture rules (absolute)

- FastAPI + Uvicorn only. No Flask, no Gunicorn.
- Cloud Run CMD: `exec uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1`
- Region: `us-central1`
- Gemini: `gemini-3.1-pro-preview`, Vertex AI location `global`
- Money handled with `decimal.Decimal`, never float
- Every write logs an audit trail (BigQuery + Open Brain)
- Cross-service calls authenticated with Google ID tokens
- No JSON key files in the runtime container; ADC only (DWD has a documented
  exception — see `monique_file_manager._get_dwd_credentials`)
- No hardcoded secrets, URLs, or tokens

## Governance chain

```
Monique Holdings (Apex)
  └─ Monique CEO (51% voting authority)
       └─ Monique Controller (12-hour cycles)
            └─ Coordinator V2 (task routing)
                 └─ Agents (workers — this service is one)
                      └─ MFM (ingestion — this service)
                           └─ Genesis Open Brain (memory)
                                └─ BigQuery (source of truth)
```

Release gates: AegisAILink + PatentAI + MoniqueCEO + CFOInsiderAI + TrustLawyerAI.
Human veto window: 24 hours.

## Local development

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

Without `OPEN_BRAIN_URL` / `NEWTON_URL` set, the Open Brain and Newton calls
become logged no-ops — health/status endpoints will work, `/sync-agent` will
return `{"status": "skipped"}` for those subsystems.

## Production env vars

See `DEPLOYMENT.md` for the full list. Critical ones:

```
AGENT_NAME=monique-file-manager
GCP_PROJECT=genesis-ai-prod-7x2k
GCP_REGION=us-central1
BQ_DATASET=genesis_shared_memory
BQ_TABLE=monique_discovered_files
GEMINI_MODEL=gemini-3.1-pro-preview
VERTEX_AI_LOCATION=global
OPEN_BRAIN_URL=https://genesis-open-brain-XXXX-uc.a.run.app
NEWTON_URL=https://newton-ai-XXXX-uc.a.run.app
DWD_IMPERSONATE_SA=dwd-delegate@<core-project>.iam.gserviceaccount.com
AGENT_REGISTRY=sharp-og=https://...,newton-ai=https://...
```

## Important notes for AI assistants

- **Never** rename `app` in `main.py` (Dockerfile CMD references it).
- **Never** add `gunicorn` to `requirements.txt`. Architecture rule.
- **Never** add `kheprahel@gmail.com` to any agent-accessible list.
- **Never** hardcode a Cloud Run URL. Use `AGENT_REGISTRY` / dedicated env vars.
- **Always** authenticate cross-service calls with `auth_helpers.authed_post` /
  `authed_get` (uses ID tokens with the right audience).
- **Always** log significant actions to Open Brain via `open_brain.memory_store`.
- Schemas in `monique_file_manager._FILE_CATALOG_SCHEMA` are authoritative for
  the `monique_discovered_files` table — verify against the live BigQuery
  table before any mass migration.
