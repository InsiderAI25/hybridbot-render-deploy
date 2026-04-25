# Monique CEO

51% voting authority + the empire's auth-token issuer. Tokens are RS256 JWTs signed via Cloud KMS asymmetric key — the private key never leaves Google's HSM.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | KMS configured / known agents / gate-voter count |
| GET | `/authority` | Authority pct + release-gate voter list |
| GET | `/.well-known/jwks.json` | Public JWK so peers verify offline |
| POST | `/auth/issue` | Caller (any KNOWN_AGENT) requests a JWT; returns `token`, `expires_in`, `jti` |
| POST | `/auth/verify` | Verify a token Monique CEO issued |
| POST | `/decide` | Open a new governance decision (returns `decision_id`) |
| POST | `/vote` | Gate voter (Aegis / Patent / CFO / TrustLawyer) casts yes/no |

A decision becomes `ratified` when every entry in `GATE_VOTER_AGENTS` has voted `yes`. A single `no` from a gate voter sets `rejected`. Monique CEO's 51% authority is asserted in the JWT payload (`monique_ceo_authority_pct: 51`), so any agent verifying the token sees the authority claim.

## Deploy

```bash
# 1. Create the KMS asymmetric signing key (one-time):
gcloud kms keyrings create monique-ceo --location=global
gcloud kms keys create governance-jwt \
  --keyring=monique-ceo --location=global \
  --purpose=asymmetric-signing \
  --default-algorithm=rsa-sign-pkcs1-2048-sha256

# 2. Grant the Monique CEO runtime SA `roles/cloudkms.signer` on the key.

# 3. Deploy:
gcloud run deploy monique-ceo \
  --source=services/monique-ceo \
  --region=us-central1 \
  --no-allow-unauthenticated \
  --service-account=monique-ceo-runtime@PROJECT.iam.gserviceaccount.com \
  --set-env-vars="\
GCP_PROJECT=genesis-ai-prod-7x2k,\
BQ_DATASET=genesis_shared_memory,\
KMS_SIGNING_KEY=projects/PROJECT/locations/global/keyRings/monique-ceo/cryptoKeys/governance-jwt/cryptoKeyVersions/1,\
JWT_ISSUER=monique-ceo,\
KNOWN_AGENTS=...comma-separated agent SA emails...,\
GATE_VOTER_AGENTS=aegis-ai-link@...,patent-ai@...,cfo-insider-ai@...,trust-lawyer-ai@..."
```

Required IAM on the runtime SA:
- `roles/cloudkms.signer` on the signing key.
- `roles/cloudkms.publicKeyViewer` on the signing key.
- `roles/bigquery.dataEditor` + `roles/bigquery.jobUser` on the BQ project.

## Agents using a Monique-CEO token

```python
# Outbound: get a token from Monique CEO (caller authenticates with its
# Cloud Run identity ID token first, then receives a Monique CEO JWT for
# scoped governance calls)
import httpx
async with httpx.AsyncClient() as client:
    r = await auth_helpers.authed_post(
        client,
        f"{MONIQUE_CEO_URL}/auth/issue",
        audience=MONIQUE_CEO_URL,
        json={"scope": "agent:write-decision", "ttl_seconds": 600},
    )
governance_token = r.json()["token"]
```
