# Relay on Sugarkube onboarding (token.place)

This guide defines the **current near-term Sugarkube model** for token.place.

## Scope and architecture (current)

Sugarkube scope is **relay-only**:

- In cluster: `relay.py` only.
- Out of cluster: `server.py`, desktop Tauri compute nodes, Macs, Windows PCs, Raspberry Pi
  GPU/AI hat nodes, and other compute nodes.
- No in-cluster backend/GPU service is required for this phase.

The relay is **operationally stateful today** because registrations, queued client messages, and
replies are held in process memory. Current operating model:

- one pod
- one Gunicorn worker process (`RELAY_WORKERS=1`)
- multiple Gunicorn threads (`RELAY_THREADS=4` by default)
- one replica
- accepted state loss if the pod restarts or is replaced

The single worker process is intentional: relay registrations and queues are in memory, so multiple
worker processes would split state. The thread count is also intentional: browser/API chat can
self-dispatch to the relay over loopback while compute-node long polls and Kubernetes probes are
active, so the one process needs enough threads for the outer chat request plus internal relay API
calls and health/readiness traffic.

Multi-replica + shared state (Redis or similar) is explicitly future work and out of scope for
this phase.

Note on upgrades: the canonical token.place Helm chart now defaults to strict single-pod rollout
behavior for relay state safety by rendering `strategy.type: Recreate` with `replicaCount: 1`.

## Artifact ownership and source of truth

token.place publishes deployable artifacts; Sugarkube owns environment values, wrappers, and
operator workflow.

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- OCI Helm chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Launch runtime alignment for v0.1.0: Git tag `v0.1.0`, chart `appVersion: "0.1.1"`, release image `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`; updated chart defaults publish as chart package version `0.1.2`
- Preferred deploy tag for staging/prod validation: immutable `main-<shortsha>`
- Canonical release tag after pushing a Git tag (example): `v0.1.0` -> `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`
- `main-latest`, `latest`, `staging`, `prod`, and `production` are mutable/convenience labels only and not staging sign-off or production promotion material
- Before publishing, run `helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.2`; if chart `0.1.2` already exists and contents are stale/mismatched, do not overwrite or re-push it; stop and decide manually. If chart `0.1.2` does not exist, proceed with publishing chart package version `0.1.2`.

## Default hostnames

- Staging default: `https://staging.token.place`
- Production default: `https://token.place`

Operators can override hostnames in Sugarkube values and Cloudflare route configuration.

## Public URL vs. internal distributed relay target

Relay-only Kubernetes deployments use two different relay URLs:

- **Public relay URL**: the browser/desktop-facing HTTPS origin, for example
  `https://staging.token.place` or `https://token.place`. The chart derives
  `TOKENPLACE_RELAY_PUBLIC_URL=https://<ingress.host>` when ingress is enabled.
- **Internal distributed relay target**: the same-pod self-dispatch URL used when API v1 browser
  chat forces the desktop-bridge E2EE route. The chart defaults
  `TOKENPLACE_RELAY_INTERNAL_URL` to `http://127.0.0.1:$(RELAY_PORT)` so the target follows the
  effective relay port while staging/prod avoid accidentally routing forced desktop-bridge
  requests through a production-like public default.

Target precedence is deliberately split between operator overrides, same-pod dispatch, and
public/config fallbacks:

1. `explicit_env:*` from `TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL`,
   `TOKENPLACE_DISTRIBUTED_RELAY_URL`, or `TOKENPLACE_DISTRIBUTED_COMPUTE_URL`. Use these only
   when an operator deliberately wants to dispatch API v1 distributed work to a specific external
   relay/compute target; forced desktop-bridge routing also lets these explicit overrides beat
   verified loopback same-origin routing.
2. `relay_internal_env:*` from `TOKENPLACE_RELAY_INTERNAL_URL`,
   `TOKEN_PLACE_RELAY_INTERNAL_URL`, or `RELAY_INTERNAL_URL`. Helm uses this class for safe
   relay-only self-dispatch diagnostics (`relay_only=True`).
3. `relay_public_env:*` from `TOKENPLACE_RELAY_PUBLIC_URL`, `TOKEN_PLACE_RELAY_PUBLIC_URL`, or
   `RELAY_PUBLIC_URL`. This is a public HTTPS relay fallback for non-loopback requests.
4. `config:*` from non-default `api.relay_url` or `relay.server_url` values.
5. `production_default` (`https://token.place`) only when `TOKEN_PLACE_ENV=production`.
6. `unset` in non-production when no trusted relay target is configured.

For forced desktop-bridge request routing, `request_override:loopback_same_origin` is inserted
after `explicit_env:*` and before `relay_internal_env:*`, `relay_public_env:*`, `config:*`, and
`production_default`. That keeps verified local desktop/browser sessions on their same-origin
local relay unless an operator deliberately set an explicit distributed target override.

Otherwise leave the chart defaults in place; staging should not require a manual Helm override for
`TOKENPLACE_RELAY_INTERNAL_URL`, `TOKENPLACE_DISTRIBUTED_COMPUTE_URL`,
`TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL`, or `RELAY_THREADS`.


## Ingress TLS expectations for staging/prod

Cloudflare Tunnel continues to route public hostnames to Traefik, while Helm values control only
Kubernetes objects. Helm does not manage Cloudflare routes, DNS, WAF, or Access policies. Treat
Cloudflare route/TLS/WAF validation as an external release gate because a desktop/compute-node
registration can be blocked before it reaches `relay.py`.

Staging/prod overlays must set `ingress.tls.enabled: true`; cert-manager annotation/secret names
alone are not enough to render `spec.tls` in the chart. Operators must verify the rendered Ingress
TLS block (`spec.tls`) before deploy.

Assumption: cert-manager is installed and the configured ClusterIssuer (for example
`letsencrypt-production`) exists.

- Staging: host `staging.token.place`, TLS secret `tokenplace-staging-tls`
- Production: host `token.place`, TLS secret `tokenplace-prod-tls`

Copy-pasteable external route checks:

~~~bash
# Cloudflare Tunnel/DNS must point these public hosts at Traefik; inspect the
# Cloudflare dashboard or your tunnel config for the exact tunnel name/UUID.
# These DNS and HTTPS probes verify the route from the public edge to the app.
dig +short staging.token.place
dig +short token.place
for host in staging.token.place token.place; do
  curl -vI "https://${host}/"
  curl -fsS "https://${host}/livez"
  curl -fsS "https://${host}/healthz"
  curl -fsS "https://${host}/relay/diagnostics"
done

# Safely reproduce the compute-node registration request shape without using
# a real desktop token or payload. This pre-app route probe is intentionally
# non-mutating: it uses a clearly fake token and an empty server_public_key so
# it cannot register a fake compute node even against tokenless relays.
HOST=staging.token.place
DIAG_TOKEN="DO_NOT_USE_REAL_TOKEN_ROUTE_PROBE"
curl -i -X POST "https://${HOST}/api/v1/relay/servers/register" \
  -H 'content-type: application/json' \
  -H "X-Relay-Server-Token: ${DIAG_TOKEN}" \
  --data '{"server_public_key":""}'

# Interpret the route probe only as Cloudflare/DNS/TLS/WAF evidence:
# - JSON 400 or 401 means the request reached relay.py and was rejected by the app.
# - A non-JSON 403 with server: cloudflare or a cf-ray header means the request
#   likely stopped in a pre-app Cloudflare/WAF/Access layer.
# Operators must not replace this pre-app route probe with a real accepted
# token or a non-empty public key. Real compute-node registration remains a
# separate sign-off gate using the actual operator/environment-specific node
# command.

# If a desktop/compute node reports a pre-app 403, capture cf-ray from the
# client log/response and filter Cloudflare Security Events by that Ray ID.
CF_RAY=REPLACE_CF_RAY
printf 'Check Cloudflare Security Events for Ray ID: %s\n' "$CF_RAY"
~~~

The route probe is intentionally diagnostic-only: operators must not replace this pre-app route
probe with a real accepted token or a non-empty public key. Real compute-node registration remains
a separate sign-off gate using the actual operator/environment-specific node command.

## Sugarkube deployment command patterns

> Run the following from a **Sugarkube checkout**, not from token.place.

Use the values files and version file that live in Sugarkube for your environment; `PATH/TO/*` placeholders below are intentionally repo-local to Sugarkube. The GHCR-first release checklist lives in [docs/ops/sugarkube-release.md](ops/sugarkube-release.md).

Current app-specific staging deploy wrapper:

~~~bash
just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
~~~

Future generic Sugarkube app wrapper, once P5 lands:

~~~bash
just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
~~~

Lower-level install pattern:

~~~bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=PATH/TO/tokenplace.values.dev.yaml,PATH/TO/tokenplace.values.staging.yaml version_file=PATH/TO/tokenplace.version default_tag=main-REPLACE_SHORTSHA
~~~

Existing release upgrade pattern:

~~~bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=PATH/TO/tokenplace.values.dev.yaml,PATH/TO/tokenplace.values.staging.yaml version_file=PATH/TO/tokenplace.version default_tag=main-REPLACE_SHORTSHA
~~~

Production pattern uses `PATH/TO/tokenplace.values.prod.yaml` with the same approved
immutable tag.

## Validation gates

Generic Sugarkube status, `app-status`, `app-verify`, `/livez`, `/healthz`,
`/relay/diagnostics`, and root HTTP checks are necessary but insufficient. Staging promotion and
production sign-off require a real external relay-compute proof:

- Staging promotion gate: a real external desktop/compute node registers to
  `https://staging.token.place` and appears in both `/healthz` and `/relay/diagnostics`.
- Staging promotion gate: a real encrypted API v1 relay/desktop-bridge E2EE request/response
  succeeds through that registered staging node.
- Production post-promotion gate: a separate real production desktop/compute-node registration and
  encrypted API v1 relay/desktop-bridge E2EE request/response succeeds against
  `https://token.place`.
- Evidence must include the immutable image tag, chart version and digest where available, rendered
  or live deployment YAML, health/diagnostics output after the compute test, and relay logs after
  the compute test.

The exact desktop/compute-node launch command is operator/environment-specific; record the command
actually used, but do not invent a universal runbook command. Plaintext relay-dispatched API v1
paths are intentionally fail-closed and are not staging or production readiness evidence.

### Staging generic HTTP checks

~~~bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
CHART_VERSION="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' PATH/TO/tokenplace.version | head -n1)"
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$CHART_VERSION" --namespace tokenplace -f PATH/TO/tokenplace.values.dev.yaml -f PATH/TO/tokenplace.values.staging.yaml --set image.tag=main-REPLACE_SHORTSHA > /tmp/tokenplace-staging-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-staging-render.yaml
grep -n "staging.token.place" /tmp/tokenplace-staging-render.yaml
grep -n "tokenplace-staging-tls" /tmp/tokenplace-staging-render.yaml
grep -n "name: TOKENPLACE_RELAY_PUBLIC_URL" -A1 /tmp/tokenplace-staging-render.yaml
grep -n "name: TOKENPLACE_RELAY_INTERNAL_URL" -A1 /tmp/tokenplace-staging-render.yaml
grep -n "name: RELAY_THREADS" -A1 /tmp/tokenplace-staging-render.yaml
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://staging.token.place/
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/relay/diagnostics
curl -fsS https://staging.token.place/
~~~

### Production generic HTTP checks

~~~bash
CHART_VERSION="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' PATH/TO/tokenplace.version | head -n1)"
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$CHART_VERSION" --namespace tokenplace -f PATH/TO/tokenplace.values.dev.yaml -f PATH/TO/tokenplace.values.prod.yaml --set image.tag=v0.1.0 > /tmp/tokenplace-prod-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-prod-render.yaml
grep -n "token.place" /tmp/tokenplace-prod-render.yaml
grep -n "tokenplace-prod-tls" /tmp/tokenplace-prod-render.yaml
grep -n "name: TOKENPLACE_RELAY_PUBLIC_URL" -A1 /tmp/tokenplace-prod-render.yaml
grep -n "name: TOKENPLACE_RELAY_INTERNAL_URL" -A1 /tmp/tokenplace-prod-render.yaml
grep -n "name: RELAY_THREADS" -A1 /tmp/tokenplace-prod-render.yaml
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://token.place/
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/relay/diagnostics
curl -fsS https://token.place/
~~~

For desktop release candidates, use the shared [desktop parity validation checklist](desktop_parity_validation.md) before making staging, production, two-node, or round-robin claims. That checklist includes copy-paste staging diagnostics, queue-depth, Stop/Start, Windows CUDA, macOS Metal, and CPU fallback commands.

### Desktop compute-node HTTP 403 / pre-app rejection diagnostics

Symptom:
- A desktop/Tauri compute node logs `desktop.compute_node_bridge.api_v1_e2ee.register`
  followed by `error=HTTP 403`, while a synthetic register/poll probe succeeds and relay pod logs
  do not show corresponding `POST /api/v1/relay/servers/register`,
  `POST /api/v1/relay/servers/unregister`, or `POST /api/v1/relay/servers/poll` requests.

Why this matters:
- The relay registration-token guard returns JSON `401` responses for invalid tokens. A non-JSON
  `403` with `server: cloudflare` or a `cf-ray` header usually means Cloudflare/WAF or another
  pre-app layer rejected the request before it reached `relay.py`.
- Desktop diagnostics intentionally log only safe routing/infrastructure metadata: method, URL
  path, status, selected response headers, a capped redacted body snippet, and whether a relay
  token was sent. They must not include the token value, private keys, public keys, ciphertext, or
  model prompts.

Operator steps:

~~~bash
# 1. Capture the cf-ray from the desktop log event, if present.
# Example event fields: status=403 server=cloudflare cf_ray=REPLACE_CF_RAY
CF_RAY=REPLACE_CF_RAY

# 2. In Cloudflare, open Security > Events for the staging zone and filter by Ray ID.
# Check which WAF, bot, firewall, or access rule produced the 403.

# 3. Reproduce the compute-node registration shape without exposing the real desktop token.
# This route-shape probe is intentionally non-mutating: it uses a clearly fake
# token and an empty server_public_key so it cannot register a fake compute node
# even if the relay is running in tokenless mode. Operators must not replace this
# pre-app route probe with a real accepted token or a non-empty public key; real
# compute-node registration remains a separate sign-off gate using the actual
# operator/environment-specific node command.
DIAG_TOKEN="DO_NOT_USE_REAL_TOKEN_ROUTE_PROBE"
curl -i -X POST https://staging.token.place/api/v1/relay/servers/register \
  -H 'content-type: application/json' \
  -H "X-Relay-Server-Token: ${DIAG_TOKEN}" \
  --data '{"server_public_key":""}'

# An app-level JSON 400 or 401 means the request reached relay.py. A non-JSON
# 403 with server: cloudflare or a cf-ray header means Cloudflare/WAF/Access
# likely blocked the request before relay.py.

# 4. Reproduce with Python requests to compare headers/body with desktop behavior.
python - <<'PY'
import requests

base_url = 'https://staging.token.place/api/v1/relay/servers'
headers = {
    'content-type': 'application/json',
    'X-Relay-Server-Token': 'DO_NOT_USE_REAL_TOKEN_ROUTE_PROBE',
}
payload = {'server_public_key': ''}
response = requests.post(f'{base_url}/register', headers=headers, json=payload, timeout=15)
print('status', response.status_code)
print('headers', {k: response.headers.get(k) for k in ['server', 'cf-ray', 'cf-cache-status', 'content-type', 'x-request-id']})
print('body', response.text[:512])
PY

# 5. Compare relay app logs with desktop diagnostics for the same UTC window.
kubectl -n tokenplace logs deploy/tokenplace --since=30m | \
  grep -E 'POST /api/v1/relay/servers/(register|unregister|poll)|api_v1|relay/servers'
~~~

Decision points:
- If desktop logs show `kind=cloudflare_pre_app_rejection`, `status=403`, and a `cf-ray`, but
  relay app logs have no matching POST, investigate Cloudflare Security Events for that Ray ID.
- If desktop logs show `kind=relay_json_error` with `status=401`, the request reached the relay;
  check registration-token configuration on the desktop and relay.
- If desktop logs show `kind=http_status_no_json_body` without Cloudflare headers, inspect ingress,
  tunnel, and upstream proxy logs before changing relay application code.

Operational note: `/healthz`, `/livez`, `/metrics`, and `/relay/diagnostics` are intentionally
exempt from the public API rate-limit quota. API v1 compute-node heartbeat/control-plane POST
routes are also exempt from the public quota after passing the relay token boundary: valid
`X-Relay-Server-Token` when tokens are configured, or the documented tokenless behavior when no
relay server tokens are configured. Invalid-token POSTs still consume the public quota and are only
charged to the aggregate client-IP control-plane bucket, not to spoofable server/client identity
buckets. A Kubernetes readiness probe that calls `/healthz` every 10 seconds must not exhaust the
default `API_RATE_LIMIT=60/hour`; before this exemption, staging could return 429 to kube-probe and
become externally unhealthy even while the relay process was running. Compute-node
register, unregister, poll, and encrypted response submission traffic is instead protected by dedicated
control-plane budgets (`API_RELAY_CONTROL_PLANE_RATE_LIMIT`,
`API_RELAY_CONTROL_PLANE_REGISTER_RATE_LIMIT`, `API_RELAY_CONTROL_PLANE_UNREGISTER_RATE_LIMIT`,
`API_RELAY_CONTROL_PLANE_POLL_RATE_LIMIT`, `API_RELAY_CONTROL_PLANE_RESPONSE_RATE_LIMIT`,
and aggregate `API_RELAY_CONTROL_PLANE_IP_RATE_LIMIT`)
so multiple authenticated desktop nodes behind one NAT can poll normally without consuming chat/user
quota. Configure `TOKENPLACE_RATE_LIMIT_STORAGE_URI` with a shared backend such as Redis or Memcached
in multi-worker deployments so public and control-plane budgets are shared across workers. User-facing
chat/completion routes remain rate-limited.

If Cloudflare/WAF skip or allow rules enumerate API v1 compute-node control paths, include
`POST /api/v1/relay/servers/unregister` alongside register and poll so Stop operator shutdown can
reach `relay.py` immediately instead of waiting for heartbeat lease expiry.

## v0.1.0 staging failure modes and operator runbook

The following failure modes were observed during v0.1.0 staging and should be treated as a
pre-launch runbook for future operators.

### 1) Stale OCI chart package (`0.1.0`) in GHCR

Symptom:
- Deployed manifests from OCI chart `0.1.0` do not include expected rollout safety defaults
  (especially `strategy.type: Recreate`), even though local chart sources do.

Why this matters:
- For relay's in-memory queue/registration state, rollout behavior must stay single-pod-safe.
- A stale chart can silently remove safety defaults and invalidate staging confidence.

Decision rule for v0.1.0:
- Because launch identifiers are intentionally pinned at `0.1.0`, pre-launch chart package
  deletion/re-publish may be required when GHCR `0.1.0` content is stale or incorrect.
- Keep this as a deliberate operator action with notes (who/when/why), not an automatic overwrite.

Verification commands:

~~~bash
# Compare local chart metadata with OCI package metadata.
helm show chart ./charts/tokenplace
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.2

# Render local and OCI manifests, then diff strategy/env sections.
# For staging candidate validation, set this to the immutable image tag deployed by Sugarkube.
IMAGE_TAG=main-REPLACE_SHORTSHA


helm template tokenplace ./charts/tokenplace --namespace tokenplace -f PATH/TO/tokenplace.values.dev.yaml -f PATH/TO/tokenplace.values.staging.yaml --set image.tag="$IMAGE_TAG" > /tmp/tokenplace-local.yaml
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.2 --namespace tokenplace -f PATH/TO/tokenplace.values.dev.yaml -f PATH/TO/tokenplace.values.staging.yaml --set image.tag="$IMAGE_TAG" > /tmp/tokenplace-oci.yaml
diff -u /tmp/tokenplace-local.yaml /tmp/tokenplace-oci.yaml | less
~~~

### 2) Missing `Recreate` strategy in deployed chart output

Symptom:
- Deployment renders/rolls out without `spec.strategy.type: Recreate`.

Verification commands:

~~~bash
grep -n "strategy:" -A4 /tmp/tokenplace-local.yaml
grep -n "strategy:" -A4 /tmp/tokenplace-oci.yaml
kubectl -n tokenplace get deploy tokenplace -o yaml | grep -n "strategy:" -A4
~~~

Expected:
- Deployment strategy shows `type: Recreate`.

### 3) Relay crash under read-only root filesystem (XDG paths)

Symptom:
- Relay container restarts/crashes when chart enforces read-only root filesystem.

Cause:
- Runtime attempted to write default XDG/config/cache data to unwritable filesystem locations.

Required fix:
- Redirect runtime write paths to `/tmp` using XDG env vars in the deployment.

Verification commands:

~~~bash
kubectl -n tokenplace get deploy tokenplace -o yaml | grep -n "XDG_CONFIG_HOME\\|XDG_CACHE_HOME\\|XDG_DATA_HOME\\|XDG_STATE_HOME" -A1 -B1
kubectl -n tokenplace logs deploy/tokenplace --tail=200
~~~

Expected:
- `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME` are set under container env.
- No read-only filesystem write failure appears in logs after rollout.

### 4) Duplicate environment variable rendering warnings

Symptom:
- Warnings/errors indicate duplicate env entries rendered from both chart defaults and values
  overlays.

Verification commands:

~~~bash
grep -n "name: TOKENPLACE_" -A1 /tmp/tokenplace-oci.yaml
python - <<'PY'
import sys, yaml
from collections import Counter

with open('/tmp/tokenplace-oci.yaml', 'r', encoding='utf-8') as f:
    docs=[d for d in yaml.safe_load_all(f) if isinstance(d, dict)]

for d in docs:
    if d.get('kind') != 'Deployment':
        continue
    for c in d.get('spec', {}).get('template', {}).get('spec', {}).get('containers', []):
        names=[e.get('name') for e in c.get('env', []) if isinstance(e, dict) and e.get('name')]
        dupes=[k for k,v in Counter(names).items() if v>1]
        if dupes:
            print(f"{d.get('metadata', {}).get('name','<unknown>')}:{c.get('name','<unknown>')} duplicates: {', '.join(dupes)}")
PY
helm upgrade --install tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.2 --namespace tokenplace --dry-run=client
~~~

Expected:
- No duplicated env variable names in rendered manifest.
- Dry-run output has no duplicate-env warnings.

### 5) Desktop external compute node long-poll timeout (staging)

Symptom:
- Compute node fails or stalls during register/poll loop against `https://staging.token.place`.

Verification focus:
- Validate end-to-end register/poll/request/reply path with an actual external node, not relay
  pod health alone.

### 6) Health checks green before true relay flow validation

Warning:
- `/livez`, `/healthz`, `/`, and `/metrics` are necessary checks but **not sufficient** for
  production sign-off.
- Sign-off requires successful encrypted relay flow with a real external compute node.

### Required external compute node validation checklist (sign-off gate)

Run this in staging before production promotion:

1. External compute node registers successfully to relay.
2. Relay `/healthz` `knownServers` increments from `0` to `>=1`.
3. `/relay/diagnostics` lists the registered compute node.
4. Client encrypted request is accepted/queued by relay.
5. Compute node receives and processes the queued request.
6. Client retrieves encrypted response successfully.

Example checks:

~~~bash
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/relay/diagnostics
curl -fsS https://staging.token.place/metrics | head -n 40
~~~

### Image tag, chart version, and Git tag must be validated independently

For final v0.1.0 release readiness, verify all four identifiers explicitly (separate from staging candidate validation):

- Git release tag: `v0.1.0`
- Chart package version: `0.1.2` (with chart `appVersion: "0.1.1"`)
- Chart appVersion: `"0.1.1"`
- Relay image tag: `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`

Example image-tag existence checks:

~~~bash
# Staging candidate validation (use the same candidate tag as IMAGE_TAG above).
IMAGE_TAG=main-REPLACE_SHORTSHA
docker manifest inspect "ghcr.io/futuroptimist/tokenplace-relay:${IMAGE_TAG}" >/dev/null

# Final release-tag validation.
docker manifest inspect ghcr.io/futuroptimist/tokenplace-relay:v0.1.0 >/dev/null
~~~

### Relay-only health output expectations (staging and production)

For Sugarkube relay-only deployments, healthy relay readiness does **not** require
`TOKENPLACE_RELAY_UPSTREAM_URL` or an in-cluster GPU service. External compute nodes can register
later.

Before (misleading in relay-only mode):

~~~json
{
  "status": "ok",
  "publicBaseUrl": "https://staging.token.place",
  "knownServers": 0,
  "configuredUpstreamServers": ["https://token.place"],
  "upstream": "http://gpu-server:3000"
}
~~~

After (explicit relay-only semantics):

~~~json
{
  "status": "ok",
  "publicBaseUrl": "https://staging.token.place",
  "relayOnly": true,
  "upstreamHealthRequired": false,
  "knownServers": 0,
  "configuredUpstreamServers": ["https://token.place"],
  "legacyConfiguredUpstreamServers": ["https://token.place"],
  "upstream": "http://gpu-server:3000",
  "details": {"knownServers": "empty"}
}
~~~

Interpretation:
- `status: ok` reflects relay process readiness.
- `knownServers: 0` means no registered external compute nodes yet (expected before node registration).
- `configuredUpstreamServers` is retained as a stable compatibility key.
- `legacyConfiguredUpstreamServers` represents compatibility/default config, not a required staging dependency.

## Guardrails

- Keep API v1 relay-blind E2EE invariants intact (ciphertext only + safe routing metadata).
- Do not treat legacy relay endpoints as active production path.
- Do not require `TOKENPLACE_RELAY_UPSTREAM_URL` for relay-only Sugarkube readiness.
- Do not use local chart path deployment (`./deploy/charts/tokenplace-relay`) for Sugarkube
  steady-state operations.
