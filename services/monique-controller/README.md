# Monique Controller

12-hour cycle orchestrator. Cloud Scheduler hits `/cycle/tick`; the controller fans out to every agent in `AGENT_REGISTRY` and records the cycle outcome in BigQuery.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Cycle config + registry size |
| POST | `/cycle/tick` | Scheduler hook (gated by `SCHEDULER_INVOKER_SAS`) |
| GET | `/cycle/status` | Last completed cycle |
| GET | `/cycle/history?limit=` | Recent cycles |

## Deploy

```bash
gcloud run deploy monique-controller \
  --source=services/monique-controller \
  --region=us-central1 \
  --no-allow-unauthenticated \
  --service-account=monique-controller-runtime@PROJECT.iam.gserviceaccount.com \
  --set-env-vars="\
GCP_PROJECT=genesis-ai-prod-7x2k,\
BQ_DATASET=genesis_shared_memory,\
CYCLE_HOURS=12,\
SCHEDULER_INVOKER_SAS=cloud-scheduler-invoker@PROJECT.iam.gserviceaccount.com,\
AGENT_REGISTRY=sharp-og=https://...,newton-ai=https://...,...,\
TMA_URL=https://technical-master-ai-XXXX-uc.a.run.app,\
MONIQUE_CEO_URL=https://monique-ceo-XXXX-uc.a.run.app"

# Cloud Scheduler job (every 12h):
gcloud scheduler jobs create http monique-controller-tick \
  --location=us-central1 \
  --schedule="0 */12 * * *" \
  --uri="$(gcloud run services describe monique-controller \
    --region=us-central1 --format='value(status.url)')/cycle/tick" \
  --http-method=POST \
  --oidc-service-account-email=cloud-scheduler-invoker@PROJECT.iam.gserviceaccount.com \
  --oidc-token-audience="$(gcloud run services describe monique-controller \
    --region=us-central1 --format='value(status.url)')"
```

Required IAM:
- Runtime SA: `roles/bigquery.dataEditor` + `roles/bigquery.jobUser`.
- Scheduler SA: `roles/run.invoker` on this service.
