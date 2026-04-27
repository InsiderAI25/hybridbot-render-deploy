# Genesis Open Brain

**Proprietary, agent-only.** Not OpenAI-style. Memory service for empire agents.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Config + allowlist size |
| POST | `/memory/store` | Append `{agent_name, event, data}` to `agent_memories` |
| GET | `/memory/retrieve?agent=&limit=` | Recent events for one agent |
| POST | `/query` | Keyword search across all memories |

All non-public endpoints require:
1. Cloud Run `roles/run.invoker` on the caller's SA (transport layer).
2. Caller's SA email present in `OPEN_BRAIN_ALLOWED_AGENTS` *or* belonging to a project in `OPEN_BRAIN_ALLOWED_PROJECTS` (app layer, defense-in-depth).

If neither env var is set the service refuses all requests with HTTP 503 — explicit fail-closed.

## Deploy

```bash
gcloud run deploy genesis-open-brain \
  --source=services/genesis-open-brain \
  --region=us-central1 \
  --no-allow-unauthenticated \
  --service-account=open-brain-runtime@PROJECT.iam.gserviceaccount.com \
  --set-env-vars="\
GCP_PROJECT=genesis-ai-prod-7x2k,\
BQ_DATASET=genesis_shared_memory,\
BQ_MEMORIES_TABLE=agent_memories,\
OPEN_BRAIN_ALLOWED_PROJECTS=genesis-ai-prod-7x2k\,insider-core-project,\
OPEN_BRAIN_ALLOWED_AGENTS=monique-file-manager@...,monique-controller@..."
```

Required IAM on the runtime SA:
- `roles/bigquery.dataEditor` and `roles/bigquery.jobUser` on `genesis-ai-prod-7x2k`.

Grant `roles/run.invoker` to each agent SA that should call the service.
