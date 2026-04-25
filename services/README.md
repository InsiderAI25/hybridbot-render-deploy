# services/

Reference implementations of the empire's core agents. Each folder is a
self-contained Cloud Run service: its own `main.py`, `Dockerfile`,
`requirements.txt`, `auth.py`, and `README.md`.

## Why this folder exists

The original "eliminate all stubs across agent ecosystem" directive named
40+ agents that live in **other repos**. The Claude Code session that
authored these files only has GitHub MCP access to
`InsiderAI25/hybridbot-render-deploy`, so it cannot edit the live agents
directly.

These are **stub-free reference implementations** of the agents
that matter most. Use them three ways:

1. **Diff** the live agent against the reference; copy missing endpoints.
2. **Replace** the live agent if the reference is sufficient.
3. **Move** the folder to a dedicated repo and deploy from there.

Each folder builds and deploys independently:

```bash
gcloud run deploy <agent> \
  --source=services/<agent> \
  --region=us-central1 \
  --no-allow-unauthenticated
```

## Inventory

| Folder | Purpose | Key endpoints |
|--------|---------|---------------|
| `genesis-open-brain/` | Proprietary agent-only memory; BQ-backed; allowlist | `/memory/{store,retrieve}`, `/query` |
| `monique-ceo/` | 51% voting authority + KMS-signed JWT issuance | `/auth/{issue,verify}`, `/decide`, `/vote`, `/.well-known/jwks.json` |
| `monique-controller/` | 12-hour cycle orchestrator | `/cycle/{tick,status,history}` |
| `technical-master-ai/` | Central audit ledger | `/audit/{log,bulk,query}` |
| `gemini-coach/` | Vertex AI `gemini-3.1-pro-preview` wrapper | `/advise`, `/review`, `/suggest-fix`, `/coach` |
| `domain-control/` | Namecheap + Cloud DNS + Unstoppable Web3 records | `/domains/inventory`, `/domains/renew`, `/dns/upsert`, `/web3/update` |
| `billing-survival/` | GCP cost + Stripe revenue + runway alerts | `/survival/snapshot`, `/survival/forecast`, `/survival/pay-bill` |
| `_agent_template/` | Skeleton for the remaining agents (Sharp OG, Newton, Aegis, Sentinel, etc.) | `/sync-agent`, `/cycle/sync`, `/audit/forward` |

## Architecture rules every service obeys

- FastAPI + Uvicorn only. No Flask, no Gunicorn.
- `Dockerfile CMD exec uvicorn main:app --host 0.0.0.0 --port $PORT --workers 1`.
- Region `us-central1`.
- All cross-service calls authenticated with Google ID tokens whose `aud`
  matches the target Cloud Run URL (`auth.py` helpers).
- All inbound calls verify the caller's ID token and check the SA email
  against an explicit allowlist env var (defense-in-depth on top of
  `roles/run.invoker`).
- No JSON key files in the runtime container — ADC only.
- BigQuery is the source of truth; Open Brain is the agent-friendly
  memory facade; TMA is the audit ledger.
- Money handled with `decimal.Decimal`, never float.
- `kheprahel@gmail.com` is **never** in any agent-accessible list.

## What this folder is NOT

- Not the production deployment. Live agents live in their own repos
  (`Monique_Governance_System`, etc. — out of MCP scope for the session
  that wrote these). Diff/copy/replace as you see fit.
- Not a monorepo runtime. Each service deploys independently.
- Not a substitute for the parent `main.py` (HybridBot/MFM). MFM stays at
  the repo root; these services are peers.

## Confidence flags (per service)

| Service | Confidence | Caveat |
|---------|------------|--------|
| genesis-open-brain | 92% | Schema for `agent_memories` is declared in code; verify against live BQ before INSERT against an existing dataset. |
| monique-ceo | 85% | JWT design is mine — algorithm RS256 via Cloud KMS, claims `iss/sub/scope/jti/iat/exp/monique_ceo_authority_pct`. If the empire already uses a different shape, adjust. KMS key must be created before deploy. |
| monique-controller | 90% | Cycle scheduling assumes Cloud Scheduler hits `/cycle/tick`. If you use Workflows or another trigger, swap the auth dependency. |
| technical-master-ai | 92% | `tma_audit_log` schema declared; verify against live BQ. |
| gemini-coach | 85% | Model name `gemini-3.1-pro-preview` and `VERTEX_AI_LOCATION=global` come from the directive. If your Vertex AI project doesn't have access to that exact model, override `GEMINI_MODEL`. |
| domain-control | 82% | Namecheap XML response parsing matches docs but unverified live; Unstoppable PATCH endpoint shape may need adjustment after first call (see service README). |
| billing-survival | 85% | Stripe charges sum + GCP billing export read are well-understood; `pay-bill` deliberately does not execute payments. |
| _agent_template | 95% | Generic skeleton; nothing controversial. |
