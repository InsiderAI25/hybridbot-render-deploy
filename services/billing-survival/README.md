# Billing Survival

The agent that answers "are we dying or growing?" Reads cost (GCP billing BQ export) and revenue (Stripe + manual revenue events in BQ), computes runway, raises alerts, and **proposes** payments — never executes them. Real-money flows require Monique CEO ratification.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Which integrations are configured |
| GET | `/survival/snapshot` | Current cost / revenue / runway over `LOOKBACK_DAYS` |
| GET | `/survival/forecast?scenarios=N` | Same as snapshot under N revenue multipliers |
| POST | `/survival/alert` | Record an alert event; if `severity=critical` and Monique CEO is configured, also opens a governance decision |
| POST | `/survival/pay-bill` | Propose a payment; opens a Monique CEO decision; **returns** a payment request for a downstream executor to submit |

All endpoints require the caller's SA email to be in `ADMIN_AGENTS`.

## Deploy

```bash
gcloud run deploy billing-survival \
  --source=services/billing-survival \
  --region=us-central1 \
  --no-allow-unauthenticated \
  --service-account=billing-survival-runtime@PROJECT.iam.gserviceaccount.com \
  --set-env-vars="\
GCP_PROJECT=genesis-ai-prod-7x2k,\
BQ_DATASET=genesis_shared_memory,\
BQ_REVENUE_TABLE=revenue_events,\
BQ_BILLING_TABLE=gcp_billing_export_v1,\
BQ_ALERTS_TABLE=billing_survival_alerts,\
LOOKBACK_DAYS=7,\
RUNWAY_ALERT_DAYS=5,\
RUNWAY_CRITICAL_DAYS=2,\
BALANCE_USD=0,\
MONIQUE_CEO_URL=https://monique-ceo-XXXX-uc.a.run.app,\
ADMIN_AGENTS=monique-ceo@...\,monique-controller@..." \
  --set-secrets="STRIPE_API_KEY=stripe-api-key:latest"
```

## Set up the GCP billing BQ export (one-time, in the billing account)

```bash
# In the GCP console: Billing → Billing export → BigQuery export → enable
# Detailed cost data export, target dataset = genesis_shared_memory.
# Once enabled, a partitioned table named gcp_billing_export_v1_<billing-account-id>
# is created daily. Update BQ_BILLING_TABLE env to match the actual table name.
```

Until the export is enabled, `/survival/snapshot` returns `cost_unavailable: true` and treats cost as 0 — runway is computed only against revenue and BALANCE_USD.

## Required IAM on the runtime SA

| Role | Granted on | Why |
|------|-----------|-----|
| `roles/bigquery.dataEditor` + `roles/bigquery.jobUser` | the BQ project | Read billing export, write revenue/alerts |
| `roles/secretmanager.secretAccessor` | `stripe-api-key` secret | Read Stripe key |
| `roles/run.invoker` | `monique-ceo` Cloud Run service | Open governance decisions |

## Recording revenue manually (e.g. wire transfer, ACH)

```bash
bq insert --project_id=genesis-ai-prod-7x2k genesis_shared_memory.revenue_events << EOF
{"event_id":"$(uuidgen)","source":"manual","amount_usd":"500.00","agent":"sharp-og","description":"weekly settlement","occurred_at":"$(date -u +%FT%TZ)"}
EOF
```

## Confidence

- Stripe charges sum: 90% — endpoint shape stable, pagination handled.
- BQ revenue / billing export reads: 90% — schema for billing export is documented; revenue table schema is mine, verify against any existing one.
- Runway math: 92% — simple linear model; extend with seasonality if you have multi-week history.
- `/survival/pay-bill` opening a Monique CEO decision: 88% — depends on Monique CEO `/decide` returning a `decision_id`.
- **`/survival/pay-bill` does NOT execute payments.** Anyone wiring this to actually send money should add a separate executor service that pulls ratified decisions from Monique CEO and calls Stripe/bank APIs. Keeping execution out of the survival agent is intentional — survival reports, governance decides, executor pays.
