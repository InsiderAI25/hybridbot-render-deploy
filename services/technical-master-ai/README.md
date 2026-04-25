# Technical Master AI (TMA)

Central audit ledger for the empire. Every agent posts significant actions here; readers query the unified history.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Audit table + writer/reader allowlist sizes |
| POST | `/audit/log` | Single-row write (`{agent, event, severity, data}`) |
| POST | `/audit/bulk` | Up to 500 rows in one call |
| GET | `/audit/query?agent=&event=&severity=&limit=` | Read recent audit rows |

Writes are accepted from any SA whose email ends in `@<project>.iam.gserviceaccount.com` for any project listed in `AUDIT_WRITE_PROJECTS`. Reads are gated by an explicit `AUDIT_READ_AGENTS` list (typically Coordinator V2, Monique Controller, Monique CEO, and human operators via a personal SA).

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
AUDIT_READ_AGENTS=coordinator-v2@...,monique-controller@...,monique-ceo@..."
```
