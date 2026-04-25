# Gemini Coach

Vertex AI `gemini-3.1-pro-preview` wrapper. Single empire-internal entry point so every coaching call is logged uniformly.

`VERTEX_AI_LOCATION=global` is required for 3.1 — the service refuses other locations by default.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Model + Vertex location + guardrails |
| POST | `/advise` | `{context, question}` → free-form coaching |
| POST | `/review` | `{decision, rationale}` → JSON `{valid, risks, recommendation}` |
| POST | `/suggest-fix` | `{error, code}` → JSON `{diagnosis, fix, confidence, changed_files}` |
| POST | `/coach` | `{directive, target_agent}` → agent-friendly translated instructions |

All endpoints require the caller's SA email to be in `KNOWN_AGENTS`.

## Deploy

```bash
gcloud run deploy gemini-coach \
  --source=services/gemini-coach \
  --region=us-central1 \
  --no-allow-unauthenticated \
  --memory=1Gi \
  --service-account=gemini-coach-runtime@PROJECT.iam.gserviceaccount.com \
  --set-env-vars="\
VERTEX_AI_PROJECT=genesis-ai-prod-7x2k,\
VERTEX_AI_LOCATION=global,\
GEMINI_MODEL=gemini-3.1-pro-preview,\
KNOWN_AGENTS=...comma-separated agent SA emails...,\
MAX_INPUT_CHARS=20000,\
MAX_OUTPUT_TOKENS=2048,\
TEMPERATURE=0.3"
```

Required IAM on the runtime SA:
- `roles/aiplatform.user` on `genesis-ai-prod-7x2k` (Vertex AI invocation).

> **Confidence note:** This service is implemented against the public Vertex AI Python SDK shape. The exact model name `gemini-3.1-pro-preview` and `VERTEX_AI_LOCATION=global` come from the Genesis directive. If the deploy fails with a model-not-found error, override `GEMINI_MODEL` to the model your project has access to and update CLAUDE.md.
