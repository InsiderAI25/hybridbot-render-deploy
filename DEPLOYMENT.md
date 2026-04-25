# Deployment Guide

This service deploys to **Cloud Run** (primary) and **Render.com** (legacy).
Same image / same source — the entry command differs only in transport
(`Dockerfile CMD` vs. `Procfile`).

## Cloud Run

### One-time setup

```bash
PROJECT=insider-core-project        # or whichever project owns the runtime
REGION=us-central1
SERVICE=hybridbot
RUNTIME_SA=hybridbot-runtime@${PROJECT}.iam.gserviceaccount.com
DWD_SA=dwd-delegate@${PROJECT}.iam.gserviceaccount.com   # see DWD setup below
PROD_PROJECT=genesis-ai-prod-7x2k

# Runtime service account
gcloud iam service-accounts create hybridbot-runtime \
  --project="$PROJECT" \
  --display-name="HybridBot Cloud Run runtime"

# BigQuery writes (cross-project: runtime is in CORE, BQ is in PROD)
gcloud projects add-iam-policy-binding "$PROD_PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/bigquery.dataEditor"
gcloud projects add-iam-policy-binding "$PROD_PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/bigquery.jobUser"

# Pub/Sub publish (target topic in PROD)
gcloud projects add-iam-policy-binding "$PROD_PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/pubsub.publisher"

# Cross-service Cloud Run invoke (Open Brain, Newton, agent registry)
# Repeat per target service — example:
gcloud run services add-iam-policy-binding genesis-open-brain \
  --project="$PROD_PROJECT" --region="$REGION" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/run.invoker"

# Keyless DWD (preferred — see AUDIT_REPORT.md F-2 for caveats)
gcloud iam service-accounts add-iam-policy-binding "$DWD_SA" \
  --project="$PROJECT" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/iam.serviceAccountTokenCreator"
```

### Build + deploy

```bash
gcloud run deploy "$SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --source=. \
  --service-account="$RUNTIME_SA" \
  --allow-unauthenticated=false \
  --memory=512Mi \
  --cpu=1 \
  --concurrency=80 \
  --timeout=3600 \
  --max-instances=5 \
  --set-env-vars="\
AGENT_NAME=monique-file-manager,\
GCP_PROJECT=${PROD_PROJECT},\
CORE_PROJECT=${PROJECT},\
PROD_PROJECT=${PROD_PROJECT},\
GCP_REGION=${REGION},\
BQ_DATASET=genesis_shared_memory,\
BQ_TABLE=monique_discovered_files,\
GEMINI_MODEL=gemini-3.1-pro-preview,\
VERTEX_AI_LOCATION=global,\
DWD_IMPERSONATE_SA=${DWD_SA},\
DWD_WORKSPACE_ACCOUNTS=admin@insideraiagency.com\,sportsai@insidersportsai.com,\
OAUTH2_GMAIL_ACCOUNTS=insidersports@gmail.com,\
LOG_LEVEL=INFO" \
  --set-secrets="\
OPEN_BRAIN_URL=open-brain-url:latest,\
NEWTON_URL=newton-url:latest,\
AGENT_REGISTRY=agent-registry:latest,\
TELEGRAM_CHANNEL_ID=telegram-channel-id:latest"
```

> **NEVER** use `--key-file` or mount a JSON key. The runtime SA is attached
> via `--service-account`; ADC inside the container uses that identity.

### Required Secret Manager entries

| Secret name           | Contents                                                      |
|-----------------------|---------------------------------------------------------------|
| `open-brain-url`      | `https://genesis-open-brain-XXXX-uc.a.run.app`                |
| `newton-url`          | `https://newton-ai-XXXX-uc.a.run.app`                         |
| `agent-registry`      | `sharp-og=https://...,newton-ai=https://...,...`              |
| `telegram-channel-id` | The channel id (e.g. `-100xxxxxxx`)                           |

Grant the runtime SA `roles/secretmanager.secretAccessor` on each secret:

```bash
for SECRET in open-brain-url newton-url agent-registry telegram-channel-id; do
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --project="$PROJECT" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor"
done
```

### DWD setup (Workspace Admin side, one-time)

1. In **Google Workspace Admin → Security → API controls → Domain-wide
   delegation** add a new client with the **Client ID** of `$DWD_SA` and the
   scopes:
   ```
   https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/gmail.readonly
   ```
2. Confirm with a smoke call:
   ```bash
   curl -X POST -H "Authorization: Bearer $TOKEN" \
     "$SERVICE_URL/scan"
   curl -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/scan/status"
   ```
   Expect `status: complete` and per-account `files: <N>` after a few minutes.

## Render.com

Render reads `Procfile` directly. No Dockerfile required, but it works either
way. Required env vars are the same as Cloud Run.

In Render dashboard → Environment:

```
AGENT_NAME=monique-file-manager
GCP_PROJECT=genesis-ai-prod-7x2k
CORE_PROJECT=insider-core-project
PROD_PROJECT=genesis-ai-prod-7x2k
GCP_REGION=us-central1
BQ_DATASET=genesis_shared_memory
BQ_TABLE=monique_discovered_files
GEMINI_MODEL=gemini-3.1-pro-preview
VERTEX_AI_LOCATION=global
OPEN_BRAIN_URL=https://genesis-open-brain-XXXX-uc.a.run.app
NEWTON_URL=https://newton-ai-XXXX-uc.a.run.app
AGENT_REGISTRY=...comma-separated name=url pairs...
DWD_WORKSPACE_ACCOUNTS=admin@insideraiagency.com,sportsai@insidersportsai.com
OAUTH2_GMAIL_ACCOUNTS=insidersports@gmail.com
DWD_SERVICE_ACCOUNT_KEY=/etc/secrets/service_account.json   # Render secret file
GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/service_account.json
LOG_LEVEL=INFO
```

For Render, keyless DWD is not available (no GCP-attached SA), so you **must**
upload the SA JSON via Render's secret-file mechanism and point both
`GOOGLE_APPLICATION_CREDENTIALS` (for ADC) and `DWD_SERVICE_ACCOUNT_KEY` at it.

## Post-deploy verification

```bash
SERVICE_URL=$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" --format='value(status.url)')

TOKEN=$(gcloud auth print-identity-token --audiences="$SERVICE_URL")

# 1. Liveness
curl -fsS -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/health" | jq .
# Expect: {"status":"ok","agent":"monique-file-manager",...}

# 2. Config snapshot + Open Brain reachability
curl -fsS -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/status" | jq .
# Expect: dependencies.open_brain_ok = true, newton_ok = true

# 3. Empire sweep (only meaningful once AGENT_REGISTRY is populated)
curl -fsS -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/empire-status" | jq .

# 4. VIE-gated sync — denied (below floor)
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_name":"smoke","action":"noop","confidence":0.5}' \
  "$SERVICE_URL/sync-agent" | jq .
# Expect: approved=false, reason mentions VIE confidence

# 5. VIE-gated sync — approved
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_name":"smoke","action":"noop","confidence":0.99}' \
  "$SERVICE_URL/sync-agent" | jq .
# Expect: approved=true, open_brain status, newton_validated boolean

# 6. Sovereign-Dispatch
curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/scan" | jq .
# Expect: 202, message "Sovereign-Dispatch started"
sleep 60
curl -fsS -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/scan/status" | jq .
# Expect: status running -> complete
```

### BigQuery audit-trail checks

```sql
-- Discovered files (last hour)
SELECT source_account, source_type, COUNT(*) AS n
FROM `genesis-ai-prod-7x2k.genesis_shared_memory.monique_discovered_files`
WHERE ingested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
GROUP BY 1, 2
ORDER BY n DESC;

-- Schema diff (run if you suspect drift)
SELECT column_name, data_type, is_nullable
FROM `genesis-ai-prod-7x2k.genesis_shared_memory.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'monique_discovered_files'
ORDER BY ordinal_position;
```

### Open Brain round-trip

```bash
OB_AUD="$OPEN_BRAIN_URL"
OB_TOKEN=$(gcloud auth print-identity-token --audiences="$OB_AUD")

curl -fsS -H "Authorization: Bearer $OB_TOKEN" \
  "$OB_AUD/memory/retrieve?agent=monique-file-manager" | jq '.events[-5:]'
# Expect to see service_started + sovereign_dispatch_complete events
```

## Rollback

```bash
gcloud run services update-traffic "$SERVICE" \
  --project="$PROJECT" --region="$REGION" \
  --to-revisions=PREVIOUS_REVISION=100
```

Render: revert the deploy from the Render dashboard.

## IAM summary

| Role                                     | Granted on                          | Why                                  |
|------------------------------------------|-------------------------------------|--------------------------------------|
| `roles/bigquery.dataEditor`              | `genesis-ai-prod-7x2k`              | Insert into `monique_discovered_files` |
| `roles/bigquery.jobUser`                 | `genesis-ai-prod-7x2k`              | BigQuery streaming inserts           |
| `roles/pubsub.publisher`                 | `genesis-ai-prod-7x2k`              | Publish completion events            |
| `roles/run.invoker`                      | each peer Cloud Run service         | Authenticated cross-service calls    |
| `roles/iam.serviceAccountTokenCreator`   | `dwd-delegate@…iam.gserviceaccount.com` | Keyless DWD impersonation       |
| `roles/secretmanager.secretAccessor`     | each Secret Manager secret          | Read injected URLs / channel ids     |
