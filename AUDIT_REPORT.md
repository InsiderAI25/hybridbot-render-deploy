# Stub-Elimination Audit Report

**Branch:** `claude/eliminate-all-stubs-AIig9`
**Repo:** `InsiderAI25/hybridbot-render-deploy`
**Scope:** This single repository only — see "Out-of-scope" section for what
this PR explicitly does **not** cover.

## Out-of-scope (cannot honestly audit from this session)

The original directive named ~40 agents across multiple repos
(`Monique_Governance_System`, etc.). The Claude Code session that produced
this PR has GitHub MCP access restricted to **one repository**:
`InsiderAI25/hybridbot-render-deploy`. Calls targeting any other repo are
denied at the tool layer.

What stays unaudited and unfixed in this PR:

- Every agent that lives in another repo (Monique CEO, TMA, Coordinator V2,
  Genesis Open Brain, VIE, Aegis, VaultKeeper, CFO Insider, Newton, Sharp OG,
  Vegas Vicks, Professor Prophet X, Data Diva Props, Rookie Rich, Ms. Ayme,
  Oracle Prime, Sentinel, Watchdog, Handler, Trust Lawyer, Clerk of Court,
  Autonomous License, Reverse Engineer, Creative Insider, Domain Architect,
  Alex Sterling, Max Quantum, Dr. Victoria Nexus, Jamie Matrix, Gaming Gurus,
  OASIS Abyss, Showcase, AI Media Composer, Billing Survival, Stripe Gateway,
  Telegram Bot Factory, Gemini Oversight, Candlemaster, etc.).
- BigQuery schema verification — no BigQuery MCP tool available in this
  session, so claims like "verify schema before INSERT" cannot be honored
  beyond the schema declared in code.
- Cloud Run service URLs — no `gcloud` MCP tool, so URLs cannot be confirmed.
- Secret Manager bindings — no Secret Manager MCP tool.

To fix those, expand MCP scope to the relevant repos and/or add a BigQuery
MCP server and a `gcloud`-aware MCP server, then re-run.

## Pre-state inventory

Three Claude branches existed on this repo, none merged to `main`:

| Branch                                       | Files                                       |
|----------------------------------------------|---------------------------------------------|
| `claude/add-agent-endpoints-ff0HJ`           | FastAPI `render_main.py`, requirements, Procfile |
| `claude/fix-monique-file-manager-fQQ2l`      | Flask `render_main.py`, `monique_file_manager.py`, `config.py`, `.gitignore`, requirements |
| `claude/add-claude-documentation-kcbqD`      | `CLAUDE.md` describing Flask hello-world    |

`main` itself contained a 166-byte Flask hello-world, a `gunicorn` Procfile,
and a 16-byte requirements.txt.

## Stubs / violations found and fixed

| # | Location (pre)                                  | Issue                                                                                       | Fix                                                                                                       | Confidence |
|---|--------------------------------------------------|---------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|------------|
| 1 | `main` `render_main.py`                          | Flask hello-world stub                                                                      | Replaced with real FastAPI app: `/`, `/health`, `/status`, `/sync-agent`, `/empire-status`, `/scan`, `/scan/status` | 95%        |
| 2 | `main` `requirements.txt`                        | `flask` + `gunicorn` only                                                                   | Replaced with pinned FastAPI/uvicorn/httpx/google-{auth,api-python-client,cloud-bigquery,cloud-pubsub}     | 95%        |
| 3 | `main` `Procfile`                                | `web: gunicorn render_main:app`                                                             | `web: uvicorn render_main:app --host 0.0.0.0 --port $PORT --workers 1`                                    | 95%        |
| 4 | `add-agent-endpoints-ff0HJ` `Procfile`           | `gunicorn render_main:app -k uvicorn.workers.UvicornWorker` (gunicorn banned)               | Replaced with pure uvicorn                                                                                | 95%        |
| 5 | `add-agent-endpoints-ff0HJ` `render_main.py`     | Hardcoded `OPEN_BRAIN_URL`, `NEWTON_URL`, `CORE_PROJECT`, `PROD_PROJECT`, `CHANNEL_ID`      | Moved to `config.py`, all env-driven; no defaults reveal infra layout                                     | 95%        |
| 6 | `add-agent-endpoints-ff0HJ` `render_main.py`     | Cross-service POSTs missing Google ID-token auth                                            | New `auth.py` with `authed_post` / `authed_get` minting ID tokens via `google.oauth2.id_token.fetch_id_token` per call audience | 92%        |
| 7 | `add-agent-endpoints-ff0HJ` `render_main.py`     | Empty `try/except` swallowing errors with `{"status": "unreachable"}` stubs                 | Structured error handling with `logger.warning(...)` + status-code surfacing; same envelope but real logs | 95%        |
| 8 | `add-agent-endpoints-ff0HJ` `render_main.py`     | Hardcoded list of 9 agents in `/empire-status`                                              | Replaced with `AGENT_REGISTRY` env var parsed by `config.agent_registry()`; concurrent probes via `asyncio.gather` | 95%        |
| 9 | `add-agent-endpoints-ff0HJ` `render_main.py`     | Project-number guess `663026475135` / `865073155062` in URL templates                       | Removed; agents now come from registry, no URL guessing                                                   | 95%        |
| 10| `fix-monique-file-manager-fQQ2l` `config.py`     | `GEMINI_MODEL=gemini-2.5-pro`                                                               | Default `gemini-3.1-pro-preview` + `VERTEX_AI_LOCATION=global`                                            | 95%        |
| 11| `fix-monique-file-manager-fQQ2l` `config.py`     | `kheprahel@gmail.com` in `OAUTH2_GMAIL_ACCOUNTS` (naming-lock violation)                    | Removed; added import-time `assert_personal_account_not_leaked()` that raises if it ever reappears        | 99%        |
| 12| `fix-monique-file-manager-fQQ2l` `monique_file_manager.py` | Deprecated `datetime.utcnow()`                                                       | Switched to `datetime.now(tz=timezone.utc)` everywhere                                                    | 99%        |
| 13| `fix-monique-file-manager-fQQ2l` `monique_file_manager.py` | Interactive OAuth2 consent flow `flow.run_local_server(port=0)` in headless service | Removed; refuses with clear error if cached refresh token absent. Refresh-token flow still works.         | 95%        |
| 14| `fix-monique-file-manager-fQQ2l` `monique_file_manager.py` | Drive scan had no retry on 429/500                                                  | Added `google.api_core.retry.Retry` predicate for `TooManyRequests`/`ServiceUnavailable`/`InternalServerError` | 90%        |
| 15| `fix-monique-file-manager-fQQ2l` `monique_file_manager.py` | DWD only via `from_service_account_file` (key file required)                        | Added keyless DWD path via `impersonated_credentials.Credentials(...).with_subject(user)`; key-file fallback retained for non-GCP runs | 88% (flagged below) |
| 16| `fix-monique-file-manager-fQQ2l` `monique_file_manager.py` | No Open Brain log on completion                                                     | Added `open_brain.memory_store(event="sovereign_dispatch_complete", ...)` after each run                  | 92%        |
| 17| `fix-monique-file-manager-fQQ2l` `monique_file_manager.py` | BQ insert errors only logged, then ignored                                          | Now raises `RuntimeError` so the orchestrator records the row in `results` and propagates to `/scan/status`| 95%        |
| 18| All branches                                      | `BQ_TABLE=file_catalog` vs directive's `monique_discovered_files`                          | Default switched to `monique_discovered_files`; existing deployments override via `BQ_TABLE` env (flagged below) | 80% (flagged below) |
| 19| `add-claude-documentation-kcbqD` `CLAUDE.md`     | Documented Flask hello-world that no longer exists                                          | Rewritten to describe the new FastAPI service, naming locks, env vars, governance chain                  | 95%        |
| 20| Repo                                              | No service-to-service auth helper anywhere                                                  | New `auth.py` exposes `fetch_id_token`, `cached_id_token`, `authed_get`, `authed_post`                    | 92%        |
| 21| Repo                                              | No Open Brain client                                                                        | New `open_brain.py` with `memory_retrieve` and `memory_store`, both tolerant of `OPEN_BRAIN_URL` being unset | 92%        |
| 22| Repo                                              | No Cloud Run image                                                                          | Added `Dockerfile` matching the directive's exact CMD, drops to non-root user, .dockerignore included     | 95%        |
| 23| Repo                                              | No deployment doc                                                                           | Added `DEPLOYMENT.md` with gcloud + Render commands, env vars, IAM roles, verification curls              | 90%        |

## Items below 90% confidence — flagged for human review

### F-1. Default BigQuery table name (item 18)

The directive specifies `monique_discovered_files`. The pre-existing code
wrote to `file_catalog`. I switched the **default** to
`monique_discovered_files` and made the table name overridable via `BQ_TABLE`,
but I cannot inspect the live BigQuery dataset to confirm:

- Whether `monique_discovered_files` already exists with the same schema
- Whether historical data lives in `file_catalog` and needs migration
- Whether downstream consumers (Coordinator V2, Open Brain) read from one or both

**Recommended manual action before merge:**
```sql
SELECT table_id, row_count, last_modified_time
FROM `genesis-ai-prod-7x2k.genesis_shared_memory.__TABLES__`
WHERE table_id IN ('monique_discovered_files', 'file_catalog');
```
If `file_catalog` is the canonical name, set `BQ_TABLE=file_catalog` in the
Cloud Run env and accept the directive default for new deployments.

### F-2. Keyless DWD via impersonated credentials (item 15)

`google.auth.impersonated_credentials.Credentials(...).with_subject(user)`
**should** work in `google-auth >= 2.16` but I have not been able to execute
this against a real DWD-enabled SA in this session. The fallback path
(`service_account.Credentials.from_service_account_file(..., subject=...)`)
is well-trodden and the existing code already used it.

**Recommended manual test (in a sandbox project before prod):**
1. Grant the Cloud Run runtime SA `roles/iam.serviceAccountTokenCreator` on a
   DWD-enabled SA.
2. Configure DWD for that SA in Workspace Admin with the requested scopes.
3. Set `DWD_IMPERSONATE_SA` and leave `DWD_SERVICE_ACCOUNT_KEY` unset.
4. Hit `POST /scan`. Watch logs for `Drive list failed for...` — if you see it,
   keyless DWD is not working and you should set `DWD_SERVICE_ACCOUNT_KEY`
   instead (the key-file path is unchanged).

### F-3. Open Brain endpoint shape (item 6)

The directive says:
- `GET /memory/retrieve`
- `POST /memory/store`

I implemented exactly that. But the prior `claude/add-agent-endpoints-ff0HJ`
branch posted to `/query` instead, suggesting Open Brain may also expose a
`/query` endpoint. I kept `/query` as the cross-service path for `/sync-agent`
context queries (separate from memory retrieval). If the actual contract
differs, both paths are isolated single call-sites and easy to retarget.

**Recommended manual test:**
```bash
# With OPEN_BRAIN_URL set:
curl -X POST "$SERVICE/sync-agent" \
  -H 'Content-Type: application/json' \
  -d '{"agent_name":"smoke-test","action":"noop","confidence":0.99}'
```
Look for `open_brain.status` in the response. `unreachable` or `error 404`
means the endpoint shape needs adjustment.

### F-4. Items the directive asked for but cannot exist in this repo

- **Gemini coaching service (`gemini-coach`)**: directive says "If no
  dedicated Gemini coaching service exists with REST endpoints, CREATE ONE".
  This is a separate Cloud Run service, not an endpoint on MFM. Creating it
  here would conflate two services. **Not done in this PR** — it belongs in
  its own repo (`gemini-coach`). I can scaffold it once the repo exists and
  MCP scope is granted.
- **Stripe / Telegram / Vertex AI generation**: those are agent-specific
  responsibilities (Sharp OG, Telegram Bot Factory, Newton). Not within this
  repo.
- **Audits of the other ~39 named agents**: see "Out-of-scope" at the top.

## What was preserved deliberately

- The `/scan` endpoint still runs Sovereign-Dispatch ingestion in a daemon
  thread rather than `BackgroundTasks`. Reason: ingestion can take 5+ minutes
  and Cloud Run's request timeout is 60 minutes max, but FastAPI's
  `BackgroundTasks` would tie up an event-loop slot. A thread is correct.
- DWD service-account JSON path retained as a fallback. Reason: see F-2.
- Banner string in `/` left as `"HybridBot Render Deployment Live"` to
  preserve external probe parity with the original Flask response.

## How to verify

After deploy (full curls + BQ queries are in `DEPLOYMENT.md`):

```bash
SERVICE_URL=https://hybridbot-XXXX-uc.a.run.app
TOKEN=$(gcloud auth print-identity-token --audiences="$SERVICE_URL")

curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/health" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/status" | jq .
curl -s -H "Authorization: Bearer $TOKEN" "$SERVICE_URL/empire-status" | jq .
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_name":"smoke","action":"noop","confidence":0.99}' \
  "$SERVICE_URL/sync-agent" | jq .
```

BigQuery audit-trail check:
```sql
SELECT ingested_at, source_account, source_type, COUNT(*) AS files
FROM `genesis-ai-prod-7x2k.genesis_shared_memory.monique_discovered_files`
WHERE ingested_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)
GROUP BY 1, 2, 3
ORDER BY ingested_at DESC;
```

Open Brain round-trip check (after `OPEN_BRAIN_URL` is set):
```bash
curl -s -H "Authorization: Bearer $(gcloud auth print-identity-token --audiences=$OPEN_BRAIN_URL)" \
  "$OPEN_BRAIN_URL/memory/retrieve?agent=monique-file-manager" | jq .
```
Expect to see `service_started` and (after a scan) `sovereign_dispatch_complete`.

## Follow-up commit: Render path retired

After the initial commit, the owner confirmed Render is no longer in use —
all deploys go to Cloud Run, and custom domains live on Namecheap and
Unstoppable Domains. A second commit on this branch:

- Deletes `Procfile` (Render-only entry).
- Renames `render_main.py` → `main.py` to match the directive's CMD spec
  (`exec uvicorn main:app ...`) and removes the legacy filename.
- Updates `Dockerfile` CMD to `main:app`.
- Removes the "Render.com" section from `DEPLOYMENT.md` and replaces it
  with a "Custom domains" section that handles two cases:
  - **Traditional DNS** (Namecheap + Unstoppable `.com`) via either Cloud
    Run domain mapping or external LB + Serverless NEG.
  - **Unstoppable Web3 TLDs** (`.crypto`, `.x`, `.nft`, etc.) which do
    not resolve via DNS and require IPFS / gateway-based strategies; the
    doc recommends keeping the agent API on `.com` and treating Web3
    TLDs as separate front-end concerns.
- Updates CLAUDE.md and AUDIT_REPORT.md to reference `main.py` and the
  Cloud-Run-only stance.

The repository name `hybridbot-render-deploy` is now historical. Renaming
the repo on GitHub is a separate manual action; nothing in the code
depends on the repo name.

## Follow-up commit: peer-service reference implementations

Owner directive: "fix Genesis Open Brain, Monique Controller, Monique CEO,
Technical Master AI, Gemini, and the other agents — make all stubs live and
real; verify Monique CEO can issue auth tokens; Open Brain is agent-only,
proprietary, not OpenAI-style."

Since this session cannot edit those services in their own repos (MCP scope
locked), the response is a `services/` tree of stub-free reference
implementations the owner can diff/copy/replace into the live agents.

| Service | Files added | Confidence |
|---------|-------------|------------|
| `services/genesis-open-brain/` | main, auth, Dockerfile, requirements, README | 92% |
| `services/monique-ceo/` | main (KMS-signed JWT, voting), auth, Dockerfile, requirements, README | 85% |
| `services/monique-controller/` | main (12h cycle), auth, Dockerfile, requirements, README | 90% |
| `services/technical-master-ai/` | main (audit ledger), auth, Dockerfile, requirements, README | 92% |
| `services/gemini-coach/` | main (Vertex AI wrapper), auth, Dockerfile, requirements, README | 85% |
| `services/_agent_template/` | main, auth, Dockerfile, requirements, README | 95% |
| `services/README.md` | inventory + per-service confidence flags | n/a |

### Specific deliverables vs the directive

- **Open Brain** — built as a closed, allowlist-enforced memory service,
  not OpenAI-style. All endpoints require both Cloud Run `roles/run.invoker`
  AND an explicit caller-SA allowlist; refuses to serve until the
  allowlist is configured.
- **Monique CEO can and does issue auth tokens** — `/auth/issue` mints
  RS256 JWTs signed via Cloud KMS asymmetric key. Public verification via
  `/.well-known/jwks.json`. Token includes the `monique_ceo_authority_pct: 51`
  claim so any verifying agent sees the authority assertion.
- **Voting** — `/decide` opens a decision; `/vote` accepts yes/no from each
  release-gate voter (Aegis + Patent + CFO + TrustLawyer). Single `no`
  rejects; all-yes ratifies.
- **Monique Controller** — `/cycle/tick` (Cloud Scheduler hook), fans out
  to every agent in `AGENT_REGISTRY` via `/cycle/sync`, records cycle
  outcome in BigQuery, posts audit row to TMA.
- **TMA** — `/audit/{log,bulk,query}` against `tma_audit_log`. Caller email
  is recorded from the verified ID token, not the body, so impersonation
  is not possible.
- **Gemini Coach** — `/advise`, `/review`, `/suggest-fix`, `/coach` over
  `gemini-3.1-pro-preview` with `VERTEX_AI_LOCATION=global`.
- **Agent template** — every other empire agent gets bootstrapped from
  `services/_agent_template/` (copy folder, fill in agent-specific block).

### What still requires human action

1. **Diff each reference against the live service** before deploying or
   replacing. No automated migration in this PR.
2. **Create the KMS signing key** before deploying Monique CEO (one-time):
   ```bash
   gcloud kms keyrings create monique-ceo --location=global
   gcloud kms keys create governance-jwt --keyring=monique-ceo \
     --location=global --purpose=asymmetric-signing \
     --default-algorithm=rsa-sign-pkcs1-2048-sha256
   ```
3. **Create BQ tables** the references touch (`agent_memories`,
   `monique_ceo_decisions`, `monique_ceo_tokens`,
   `monique_controller_cycles`, `tma_audit_log`). The services auto-create
   them on first write, but if the dataset already has differently-shaped
   tables under those names, the inserts will error — drop or rename the
   conflicting tables first.

## Sign-off

Total fixes applied: **23 (MFM) + Render retirement + 6 peer-service references**
Confidence ≥ 90%: 20 (MFM) + Render edits + 4 services (Open Brain, Controller, TMA, agent template)
Confidence 80–89% (flagged): 3 (F-1, F-2, F-3) + 2 services (Monique CEO JWT shape, Gemini Coach model assumption)
Out-of-scope items requiring expanded access: live editing of agents in other repos. See top of file.
