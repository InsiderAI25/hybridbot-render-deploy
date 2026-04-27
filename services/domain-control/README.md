# Domain Control

The empire's DNS / domain-registrar agent. Lives in `genesis-ai-domain-control`. Talks to:

- **Namecheap API** — domain inventory, renewal.
- **Cloud DNS** — A/AAAA/CNAME/TXT upserts on Cloud-DNS-authoritative zones (used by Cloud Run domain mappings + load balancers).
- **Unstoppable Domains REST API** — updating Web3 records (IPFS hash, redirect URL, social links) on `.crypto`/`.x`/`.nft` domains we own. **Wallet-signed transactions for ownership transfers are NOT done here** — they need the owning wallet and live off-platform.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/status` | Which integrations are configured (degraded mode if missing) |
| GET | `/domains/inventory` | All Namecheap domains + Web3 resolutions for `MANAGED_DOMAINS` |
| POST | `/domains/renew` | Namecheap renewal: `{domain, years}` |
| GET | `/dns/records?zone=` | List records in a Cloud DNS zone |
| POST | `/dns/upsert` | Create/replace an A/AAAA/CNAME/TXT record |
| POST | `/web3/update` | Set IPFS hash / redirect URL on an Unstoppable Web3 domain |

All endpoints require the caller's SA email to be in `ADMIN_AGENTS`.

## Deploy (target: genesis-ai-domain-control project)

```bash
gcloud run deploy domain-control \
  --source=services/domain-control \
  --region=us-central1 \
  --no-allow-unauthenticated \
  --service-account=domain-control-runtime@genesis-ai-domain-control.iam.gserviceaccount.com \
  --set-env-vars="\
CLOUD_DNS_PROJECT=genesis-ai-domain-control,\
MANAGED_DOMAINS=insideraiagency.com\,insidersportsai.com\,monique.crypto,\
ADMIN_AGENTS=monique-ceo@...\,monique-controller@...\,sportsai-runtime@..." \
  --set-secrets="\
NAMECHEAP_API_KEY=namecheap-api-key:latest,\
UNSTOPPABLE_API_KEY=unstoppable-api-key:latest" \
  --update-env-vars="\
NAMECHEAP_API_USER=InsiderAI25,\
NAMECHEAP_USERNAME=InsiderAI25,\
NAMECHEAP_CLIENT_IP=34.42.100.71"
```

## Required IAM on the runtime SA

| Role | Granted on | Why |
|------|-----------|-----|
| `roles/dns.admin` | `genesis-ai-domain-control` | Upsert Cloud DNS records |
| `roles/secretmanager.secretAccessor` | `namecheap-api-key`, `unstoppable-api-key` | Read API keys |

## Secrets to create

```bash
echo -n "<your-namecheap-api-key>" | gcloud secrets create namecheap-api-key \
  --project=genesis-ai-domain-control --data-file=-
echo -n "<your-unstoppable-api-key>" | gcloud secrets create unstoppable-api-key \
  --project=genesis-ai-domain-control --data-file=-
```

## Namecheap setup notes

- Whitelist the Cloud Run egress IP (production IP `34.42.100.71` per CLAUDE.md) in Namecheap's API access settings, otherwise calls 403.
- Generate the API key in Namecheap → Profile → Tools → Namecheap API Access. The key in env must match.

## Confidence

- Namecheap inventory + renew: 88% — XML response parsing matches docs but I haven't run against a live account in this session.
- Cloud DNS upsert: 92% — standard `google-cloud-dns` shape.
- Unstoppable resolve: 85% — endpoint shape from docs; verify with a single GET against a known domain after deploy.
- Unstoppable record update: 75% — the PATCH endpoint shape varies; if 4xx on first call, check Unstoppable's current API docs and adjust the payload key (some versions use `meta` vs `records`). Wallet-signed ownership ops are out of scope for this service entirely.
