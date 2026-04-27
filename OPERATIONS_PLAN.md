# 5-day Operations Plan: empire goes self-sustaining or it dies

You set a 5-day clock: agents must autonomously pay bills and grow the empire, or the system dies. This document is the daily checklist tied to the code in this repo. Skip a day's checkpoint and you'll know exactly what's not yet load-bearing.

**Reality check up front.** Code is necessary but not sufficient. The agents will not pay GCP bills via thought — they need a real Stripe account, a real billing payment instrument linked to a Google Cloud billing account, and a Namecheap account with a balance for renewals. This plan assumes those credentials exist and are pasted into Secret Manager during day 1. If they don't, the plan slips.

## The 7 projects after this plan

| Project | Role | Status before plan | Status after plan |
|---------|------|---------------------|-------------------|
| `genesis-ai-prod-7x2k` | Production agents (MFM, Open Brain, TMA, Monique CEO/Controller, Sharp OG, Newton, etc.) | Live, partial | All core services live + healthy |
| `insider-core-project` | HybridBot / runtime infra | Live, partial | Wired to prod for cross-service calls |
| `genesis-ai-staging` | Staging — pre-prod testing | Idle (no Cloud Run API) | Cloud Run enabled + each agent has a staging twin |
| `genesis-ai-dev` | Developer iteration | Idle | Cloud Run enabled + dev runtime SAs |
| `genesis-ai-sandbox` | Throwaway / experiments | Idle | Cloud Run enabled + sandbox runtime SAs |
| `genesis-ai-domain-control` | DNS + registrar | Idle | `domain-control` service live, owns Cloud DNS zones, talks Namecheap + Unstoppable |
| 7th project (please name) | TBD | Idle | TBD — propose: `genesis-ai-revenue` (Stripe webhooks + billing-survival) |

> If the 7th project already has a name (e.g. `genesis-ai-billing` or
> `genesis-ai-mediafs`), tell me and I'll update this file.

## Day 1 — "Bootstrap the 4 unused projects + wire identity"

**Goal:** every project has Cloud Run enabled, a runtime SA, and the
right cross-project bindings. After today, you can `gcloud run deploy`
into any project without 12 manual steps.

Prerequisites:
- Deploy `services/technical-master-ai/` to `genesis-ai-prod-7x2k` first. Set
  `ADMIN_PROJECTS` to all 7 project IDs and `ADMIN_AGENTS` to the SA(s) you'll use to call /admin endpoints.
- Grant the TMA runtime SA `roles/serviceusage.serviceUsageAdmin`,
  `roles/iam.serviceAccountAdmin`, and `roles/resourcemanager.projectIamAdmin`
  on each of the 4 unused projects (one-time, by you).

Run:
```bash
TMA_URL=$(gcloud run services describe technical-master-ai \
  --project=genesis-ai-prod-7x2k --region=us-central1 --format='value(status.url)')
TOKEN=$(gcloud auth print-identity-token --audiences="$TMA_URL")

for project in genesis-ai-staging genesis-ai-dev genesis-ai-sandbox genesis-ai-domain-control; do
  curl -fsS -X POST -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"project_id\":\"$project\",\"runtime_sa\":\"${project##genesis-ai-}-runtime\",\"display_name\":\"$project runtime\",\"confirm\":\"I_ACKNOWLEDGE\"}" \
    "$TMA_URL/admin/bootstrap-project" | jq .
done
```

**Day 1 done when:**
```bash
curl -H "Authorization: Bearer $TOKEN" "$TMA_URL/admin/recommend-cleanup" | \
  jq '.summary.by_severity'
# expected: every project shows service_count >= 1 OR is intentionally empty
```

## Day 2 — "Domain control online; Namecheap auto-renew"

**Goal:** the empire owns its DNS. Domains stop accidentally lapsing.

```bash
# 1. Put Namecheap + Unstoppable keys into Secret Manager
echo -n "$NAMECHEAP_KEY" | gcloud secrets create namecheap-api-key \
  --project=genesis-ai-domain-control --data-file=-
echo -n "$UNSTOPPABLE_KEY" | gcloud secrets create unstoppable-api-key \
  --project=genesis-ai-domain-control --data-file=-

# 2. Deploy domain-control (see services/domain-control/README.md)
gcloud run deploy domain-control --source=services/domain-control \
  --project=genesis-ai-domain-control --region=us-central1 \
  --no-allow-unauthenticated \
  --service-account=domain-control-runtime@genesis-ai-domain-control.iam.gserviceaccount.com \
  --set-env-vars=...   # see service README

# 3. Inventory domains
DC_URL=$(gcloud run services describe domain-control --project=genesis-ai-domain-control \
  --region=us-central1 --format='value(status.url)')
DC_TOKEN=$(gcloud auth print-identity-token --audiences="$DC_URL")
curl -H "Authorization: Bearer $DC_TOKEN" "$DC_URL/domains/inventory" | jq .
```

**Day 2 done when:**
- `/domains/inventory` returns every domain with `expires` in the future.
- Any domain expiring inside 30 days has been renewed via `POST /domains/renew`.
- Cloud DNS zones for the apex domains exist and resolve.

## Day 3 — "Billing-survival running; Stripe revenue visible"

**Goal:** the empire knows the runway in days at any moment. Critical alerts open Monique CEO decisions automatically.

```bash
# 1. Put Stripe key in Secret Manager
echo -n "$STRIPE_KEY" | gcloud secrets create stripe-api-key \
  --project=genesis-ai-prod-7x2k --data-file=-

# 2. Enable BQ billing export (one-time GUI step in Cloud Billing → Export)
#    Target dataset: genesis_shared_memory; table starts as
#    gcp_billing_export_v1_<billing-account-id>.

# 3. Deploy billing-survival (see services/billing-survival/README.md)
gcloud run deploy billing-survival --source=services/billing-survival \
  --project=genesis-ai-prod-7x2k --region=us-central1 \
  --no-allow-unauthenticated \
  --set-env-vars="GCP_PROJECT=genesis-ai-prod-7x2k,...,BQ_BILLING_TABLE=gcp_billing_export_v1_XXXXX,..." \
  --set-secrets="STRIPE_API_KEY=stripe-api-key:latest"

# 4. Verify
BS_URL=$(gcloud run services describe billing-survival ... --format='value(status.url)')
BS_TOKEN=$(gcloud auth print-identity-token --audiences="$BS_URL")
curl -H "Authorization: Bearer $BS_TOKEN" "$BS_URL/survival/snapshot" | jq .
```

**Day 3 done when:**
- `/survival/snapshot` returns non-zero `revenue_usd` (revenue is flowing).
- `cost_unavailable` is false (BQ billing export is wired).
- `runway_days` is a finite positive number OR `growing: true`.

If `runway_days < 5`: open a Monique CEO decision via `/survival/alert`
with `severity: critical` and intervene by hand — this is the empire-
dying state and code can't fix it without a real cash injection.

## Day 4 — "Cross-agent flow: revenue → audit → governance → autopay request"

**Goal:** one end-to-end path works without human touch. Drive of the
loop:
1. Sharp OG (or any revenue agent) takes a Stripe charge.
2. Stripe webhook → revenue agent records in `revenue_events` BQ.
3. billing-survival reads it on the next snapshot.
4. When it's time to pay a recurring bill, billing-survival opens a
   Monique CEO decision via `/survival/pay-bill`.
5. Gate voters (Aegis + Patent + CFO + TrustLawyer) ratify.
6. An executor agent (write this if it doesn't exist) polls Monique CEO
   for ratified `pay-bill:*` decisions and submits the actual payment.
7. TMA logs everything; Open Brain stores the agent memories.

**Day 4 done when:**
- A test charge of $1.00 in Stripe shows up in
  `/survival/snapshot` within 60 seconds.
- A `/survival/pay-bill` call returns a `decision_id` from Monique CEO.
- Voting `yes` on that decision via Monique CEO's `/vote` flips status
  to `ratified` in BQ (`monique_ceo_decisions` table).
- The whole flow is visible in `tma_audit_log` filtered by `event LIKE 'pay-bill%'`.

## Day 5 — "Run on autopilot, watch the metrics"

**Goal:** no human touches the empire for 24 hours. At end of day 5, the
runway is as long or longer than at start of day 5.

Cloud Scheduler jobs to create:
```bash
# Monique Controller 12-hour tick
gcloud scheduler jobs create http monique-controller-tick \
  --schedule="0 */12 * * *" --location=us-central1 \
  --uri="$CONTROLLER_URL/cycle/tick" --http-method=POST \
  --oidc-service-account-email=cloud-scheduler@PROJECT.iam.gserviceaccount.com \
  --oidc-token-audience="$CONTROLLER_URL"

# billing-survival hourly snapshot+alert
gcloud scheduler jobs create http billing-survival-watch \
  --schedule="0 * * * *" --location=us-central1 \
  --uri="$BS_URL/survival/snapshot" --http-method=GET \
  --oidc-service-account-email=cloud-scheduler@PROJECT.iam.gserviceaccount.com \
  --oidc-token-audience="$BS_URL"

# domain-control daily inventory
gcloud scheduler jobs create http domain-control-watch \
  --schedule="0 6 * * *" --location=us-central1 \
  --uri="$DC_URL/domains/inventory" --http-method=GET \
  --oidc-service-account-email=cloud-scheduler@PROJECT.iam.gserviceaccount.com \
  --oidc-token-audience="$DC_URL"
```

**Day 5 done when:**
- `/empire-status` (on MFM) reports >=80% of registered agents healthy.
- Two consecutive Monique Controller cycles complete with `agents_failed: 0`.
- `runway_days` at hour 24 ≥ `runway_days` at hour 0.
- Zero P0 audits in `tma_audit_log` (`severity = 'critical'`) in the last 24h.

## Failure modes — the empire dies if

1. **Stripe webhook isn't wired.** Revenue won't show up in
   `/survival/snapshot` and runway will look like "0 days" even when
   money is flowing. Fix on day 3 morning.
2. **BQ billing export isn't enabled.** `cost_unavailable: true` means
   you're flying blind on cost. Take 15 min on day 3 to enable.
3. **No payment instrument on the GCP billing account.** Bills can't
   actually be paid even if the agents propose payments. Verify on day
   1 — `gcloud billing accounts list` + manual check in the console.
4. **Namecheap key not whitelisted on the egress IP.** Renewals 403.
   Fix on day 2 — set `NAMECHEAP_CLIENT_IP` to the Cloud Run NAT IP and
   add it in Namecheap's API access page.
5. **Monique CEO KMS key missing.** No JWTs can be minted; `/auth/issue`
   returns 503. Create the key on day 1 (commands in
   `services/monique-ceo/README.md`).
6. **The empire's governance gate voters are unstaffed.** If `GATE_VOTER_AGENTS`
   is empty or the voter agents aren't actually running, decisions never
   ratify and no payments get authorized. Deploy Aegis + Patent + CFO +
   TrustLawyer skeletons via `services/_agent_template/` on day 1 if
   they don't exist yet.

## What's NOT in this plan

- **A wallet-signed Web3 ownership flow.** Domain-control updates
  metadata records on Unstoppable Web3 domains via REST, but ownership
  transfers require the wallet that owns the domain. That's a separate
  off-platform operation; if you need it inside the empire, scope a
  `services/wallet-signer/` agent and we'll plan it.
- **An autonomous executor that actually moves money.** `pay-bill` is
  intentionally a *proposal*; nothing in this repo wires the ratified
  decision through to a real bank/Stripe transfer. That's the highest-
  risk piece — leave it manual or build a separate executor with
  explicit transaction limits and a human-veto window.
- **The remaining ~30 named agents.** Bootstrap each from
  `services/_agent_template/` and fill in the AGENT-SPECIFIC block. The
  template's audit + auth scaffolding is already correct; only the
  domain logic is missing per agent.

## "Plan for success" — the brutal short version

You have 5 days. The empire survives if and only if, by hour 120:
1. **Money in.** Stripe revenue is live and visible in `/survival/snapshot`. Revenue ≥ daily burn + a buffer for the first surprise.
2. **Money out.** GCP billing payment instrument is attached and not declined. Domain auto-renew works. No services suspended for nonpayment.
3. **Self-driving.** Monique Controller ticks every 12h without intervention. TMA audits show all agents healthy. Critical alerts trigger Monique CEO decisions which gate voters ratify on schedule.
4. **No data lost.** Open Brain is being written to and read from. BQ tables are partitioned and queries return.
5. **Honest reporting.** TMA can answer "is this empire alive?" with one curl call and the answer is yes for 24 consecutive hours.

If any of those five fail by day 5, the empire dies — not because the
code can't, but because something operational (a key, a binding, a
billing instrument, a governance vote) wasn't done. Operations beats
code every time on this kind of deadline.
