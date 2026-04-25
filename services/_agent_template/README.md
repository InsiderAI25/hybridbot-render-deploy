# Agent template

Skeleton for the empire's other agents (Sharp OG, Newton, Aegis, Sentinel, etc.). Copy this folder, rename, replace the `AGENT-SPECIFIC` block in `main.py`, and deploy.

## What you get out of the box

- `/health`, `/status`
- `/sync-agent` (Coordinator V2 hook)
- `/cycle/sync` (Monique Controller hook)
- `/audit/forward` (helper that POSTs to TMA's `/audit/log` with the agent's identity)

Caller-allowlist enforcement via `KNOWN_CALLERS`. Inbound IDs are verified against Google's certs; spoofing requires a forged Google-signed JWT.

## Bootstrap a new agent

```bash
NEW_AGENT=sharp-og
cp -r services/_agent_template services/$NEW_AGENT
# Edit services/$NEW_AGENT/main.py — fill in agent-specific endpoints below
# the AGENT-SPECIFIC marker.
# Update services/$NEW_AGENT/requirements.txt with any extra deps.
# Update services/$NEW_AGENT/README.md with the new contract.

gcloud run deploy "$NEW_AGENT" \
  --source=services/"$NEW_AGENT" \
  --region=us-central1 \
  --no-allow-unauthenticated \
  --service-account="${NEW_AGENT}-runtime@PROJECT.iam.gserviceaccount.com" \
  --set-env-vars="\
AGENT_NAME=$NEW_AGENT,\
TMA_URL=https://technical-master-ai-XXXX-uc.a.run.app,\
OPEN_BRAIN_URL=https://genesis-open-brain-XXXX-uc.a.run.app,\
KNOWN_CALLERS=coordinator-v2@...,monique-controller@...,monique-ceo@..."
```

## What to NOT do

- Do not call OpenAI / external LLM APIs directly. Route AI calls through `gemini-coach`.
- Do not bypass `/audit/forward` for significant actions — every state change must hit TMA.
- Do not store memory locally. Use Genesis Open Brain.
- Do not add public unauthenticated endpoints. The empire is closed by default.
