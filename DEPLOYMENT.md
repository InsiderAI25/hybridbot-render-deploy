# Deployment Guide

This service deploys to **Cloud Run** in `us-central1`. Render is no longer
supported — the legacy Procfile path was removed. Custom domains live on
Namecheap and Unstoppable Domains; mapping is described in the "Custom
domains" section below.

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

## Custom domains

You hold domains on **Namecheap** and **Unstoppable Domains**. They split
into two completely different mapping paths because Unstoppable sells two
classes of TLD:

| TLD class | Examples | Resolution | Path to Cloud Run |
|-----------|----------|------------|-------------------|
| Traditional DNS | `.com`, `.net`, `.org`, `.io`, `.ai`, plus the `.com` domains you bought through Unstoppable | ICANN / public DNS | A/AAAA or CNAME → Cloud Run |
| Unstoppable Web3 | `.crypto`, `.x`, `.nft`, `.wallet`, `.bitcoin`, `.dao`, `.888`, `.zil`, `.blockchain`, `.polygon` | Polygon / Ethereum smart contracts | Cannot use plain A records — see Web3 section |

### Path A: Traditional DNS (Namecheap + Unstoppable .com)

Two viable options. Pick **one** per domain.

#### Option A1 — Cloud Run domain mapping (simplest)

Best when you want one Cloud Run service per domain, no LB, automatic
Google-managed TLS.

```bash
DOMAIN=app.example.com           # the FQDN you want
SERVICE=hybridbot                # the Cloud Run service
PROJECT=insider-core-project
REGION=us-central1

# 1. Verify the apex domain in Search Console (one-time, per apex):
#    https://search.google.com/search-console
#    Add a TXT record at example.com per the verification instructions.

# 2. Create the mapping:
gcloud beta run domain-mappings create \
  --project="$PROJECT" \
  --region="$REGION" \
  --service="$SERVICE" \
  --domain="$DOMAIN"

# 3. Read the records you need to add at the registrar:
gcloud beta run domain-mappings describe \
  --project="$PROJECT" --region="$REGION" --domain="$DOMAIN" \
  --format='value(status.resourceRecords)'
# This returns CNAME (subdomain) or A/AAAA (apex) target values.
```

Then in Namecheap (Advanced DNS) — or Unstoppable's DNS panel for a `.com`
bought there — add the records exactly as shown:
- For a subdomain (`app.example.com`): one **CNAME** → `ghs.googlehosted.com`
- For an apex (`example.com`): four **A** records + four **AAAA** records pointing at Google's anycast IPs (Cloud Run lists the exact IPs in step 3).

> Note: Cloud Run domain mapping in `us-central1` is generally available;
> if your account still sees it as preview, fall back to Option A2.

#### Option A2 — External HTTPS Load Balancer + Serverless NEG

Best when you want one domain to fan out to multiple Cloud Run services,
need Cloud Armor / IAP / CDN, or want a single static anycast IP.

```bash
PROJECT=insider-core-project
REGION=us-central1
SERVICE=hybridbot
DOMAIN=app.example.com

# 1. Reserve a global static IP
gcloud compute addresses create hybridbot-ip \
  --project="$PROJECT" --global

LB_IP=$(gcloud compute addresses describe hybridbot-ip \
  --project="$PROJECT" --global --format='value(address)')

# 2. Serverless NEG → Cloud Run
gcloud compute network-endpoint-groups create hybridbot-neg \
  --project="$PROJECT" \
  --region="$REGION" \
  --network-endpoint-type=serverless \
  --cloud-run-service="$SERVICE"

# 3. Backend service
gcloud compute backend-services create hybridbot-backend \
  --project="$PROJECT" --global --load-balancing-scheme=EXTERNAL_MANAGED
gcloud compute backend-services add-backend hybridbot-backend \
  --project="$PROJECT" --global \
  --network-endpoint-group=hybridbot-neg \
  --network-endpoint-group-region="$REGION"

# 4. URL map + target proxy + Google-managed cert
gcloud compute url-maps create hybridbot-urlmap \
  --project="$PROJECT" --default-service=hybridbot-backend
gcloud compute ssl-certificates create hybridbot-cert \
  --project="$PROJECT" --domains="$DOMAIN" --global
gcloud compute target-https-proxies create hybridbot-https-proxy \
  --project="$PROJECT" --url-map=hybridbot-urlmap \
  --ssl-certificates=hybridbot-cert
gcloud compute forwarding-rules create hybridbot-https-forwarding-rule \
  --project="$PROJECT" --global \
  --target-https-proxy=hybridbot-https-proxy \
  --address="$LB_IP" --ports=443

echo "Point $DOMAIN at $LB_IP"
```

In Namecheap (or Unstoppable's traditional-DNS panel) add a single **A**
record pointing at `$LB_IP`. Google issues the managed cert once the
domain resolves to the LB.

### Path B: Unstoppable Web3 domains (.crypto, .x, .nft, etc.)

These do **not** resolve through DNS. They are records on Polygon /
Ethereum that store wallet addresses or, for websites, an IPFS hash. You
can't put `mfm.crypto` in a CNAME and have a normal browser load Cloud
Run.

Three real options — be deliberate about which you pick:

1. **Don't use Web3 TLDs for the agent API.** Keep `.crypto` / `.x` for
   wallet / payment surfaces only and serve all HTTP from your traditional
   `.com` domains via Path A. **Recommended for back-end services like
   this one** — it stays on standard TLS chains and is reachable from any
   browser.
2. **Static front-end on IPFS, API on Cloud Run.** Build a static site,
   pin it to IPFS, set the **IPFS hash** in the Unstoppable record. The
   site can `fetch()` the Cloud Run service at its `.com` URL. Browsers
   need the Unstoppable browser extension or a gateway like
   `https://<domain>.unstoppable.id` to resolve the Web3 domain. Cloud
   Run is unaffected.
3. **HTTP gateway record.** If Unstoppable has set a `dweb.link` /
   `link.poof.io`-style HTTP redirect on a TLD, you can point the
   redirect at your Cloud Run service URL. This is gateway-dependent and
   not all Web3 TLDs support it. Verify in the Unstoppable dashboard
   before relying on it.

> If you want to expose the agent API on a `.crypto`-style domain, tell me
> which TLD and which front-end strategy (1, 2, or 3) you want and I'll
> write the exact config. Without that, the safe default is **Path A on
> Namecheap and on the Unstoppable .com domains**, and treat Web3 TLDs as
> separate projects.

### Notes specific to your registrars

- **Namecheap**: Advanced DNS panel; A and CNAME records propagate within
  ~30 min. Disable Namecheap's "URL redirect" / "Parking" record if it's
  set on the same hostname or it'll override your A record.
- **Unstoppable Domains (traditional .com)**: their DNS panel calls it
  "Website records." Same A/CNAME shape as Namecheap. NS-takeover is also
  available if you want Cloud DNS authoritative — point Unstoppable's NS
  records at Cloud DNS and manage everything in GCP.
- **Email MX**: Cloud Run domain mapping does not touch MX. Keep your
  Workspace MX records (`SMTP.GOOGLE.COM` family) in the registrar's DNS
  panel; the mapping only adds A/AAAA/CNAME for the web hostname.

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

## IAM summary

| Role                                     | Granted on                          | Why                                  |
|------------------------------------------|-------------------------------------|--------------------------------------|
| `roles/bigquery.dataEditor`              | `genesis-ai-prod-7x2k`              | Insert into `monique_discovered_files` |
| `roles/bigquery.jobUser`                 | `genesis-ai-prod-7x2k`              | BigQuery streaming inserts           |
| `roles/pubsub.publisher`                 | `genesis-ai-prod-7x2k`              | Publish completion events            |
| `roles/run.invoker`                      | each peer Cloud Run service         | Authenticated cross-service calls    |
| `roles/iam.serviceAccountTokenCreator`   | `dwd-delegate@…iam.gserviceaccount.com` | Keyless DWD impersonation       |
| `roles/secretmanager.secretAccessor`     | each Secret Manager secret          | Read injected URLs / channel ids     |
