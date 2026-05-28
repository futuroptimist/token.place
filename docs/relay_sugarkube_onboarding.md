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
- one Gunicorn worker
- one replica
- accepted state loss if the pod restarts or is replaced

Multi-replica + shared state (Redis or similar) is explicitly future work and out of scope for
this phase.

Note on upgrades: the canonical token.place Helm chart now defaults to strict single-pod rollout
behavior for relay state safety by rendering `strategy.type: Recreate` with `replicaCount: 1`.

## Artifact ownership and source of truth

token.place publishes deployable artifacts; Sugarkube owns environment values, wrappers, and
operator workflow.

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- OCI Helm chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Launch version alignment for v0.1.0: Git tag `v0.1.0`, chart package version `0.1.0`, chart `appVersion: "0.1.0"`, release image `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`
- Preferred deploy tag for staging/prod validation: immutable `main-<shortsha>`
- Canonical release tag after pushing a Git tag (example): `v0.1.0` -> `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`
- `main-latest` is convenience-only and not production sign-off material
- Before publishing, run `helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.0`; if chart `0.1.0` already exists and contents are stale/mismatched, do not overwrite or re-push it; stop and decide manually. If chart `0.1.0` does not exist, proceed with publishing chart package version `0.1.0`.

## Default hostnames

- Staging default: `https://staging.token.place`
- Production default: `https://token.place`

Operators can override hostnames in Sugarkube values and Cloudflare route configuration.


## Ingress TLS expectations for staging/prod

Cloudflare Tunnel continues to route public hostnames to Traefik, while Helm values control only
Kubernetes objects. Helm does not manage Cloudflare routes.

Staging/prod overlays must set `ingress.tls.enabled: true`; cert-manager annotation/secret names
alone are not enough to render `spec.tls` in the chart. Operators must verify the rendered Ingress
TLS block (`spec.tls`) before deploy.

Assumption: cert-manager is installed and the configured ClusterIssuer (for example
`letsencrypt-production`) exists.

- Staging: host `staging.token.place`, TLS secret `tokenplace-staging-tls`
- Production: host `token.place`, TLS secret `tokenplace-prod-tls`

## Sugarkube deployment command patterns

> Run the following from a **Sugarkube checkout**, not from token.place.

Use the values files and version file that live in Sugarkube for your environment; `PATH/TO/*` placeholders below are intentionally repo-local to Sugarkube.

First install pattern:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=PATH/TO/tokenplace.values.dev.yaml,PATH/TO/tokenplace.values.staging.yaml version_file=PATH/TO/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Existing release upgrade pattern:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=PATH/TO/tokenplace.values.dev.yaml,PATH/TO/tokenplace.values.staging.yaml version_file=PATH/TO/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Production pattern uses `PATH/TO/tokenplace.values.prod.yaml` with the same approved
immutable tag.

## Validation (staging example)

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
CHART_VERSION="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' PATH/TO/tokenplace.version | head -n1)"
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$CHART_VERSION" --namespace tokenplace -f PATH/TO/tokenplace.values.dev.yaml -f PATH/TO/tokenplace.values.staging.yaml --set image.tag=main-REPLACE_SHORTSHA > /tmp/tokenplace-staging-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-staging-render.yaml
grep -n "staging.token.place" /tmp/tokenplace-staging-render.yaml
grep -n "tokenplace-staging-tls" /tmp/tokenplace-staging-render.yaml
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://staging.token.place/
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

Production validation:

```bash
CHART_VERSION="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' PATH/TO/tokenplace.version | head -n1)"
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$CHART_VERSION" --namespace tokenplace -f PATH/TO/tokenplace.values.dev.yaml -f PATH/TO/tokenplace.values.prod.yaml --set image.tag=v0.1.0 > /tmp/tokenplace-prod-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-prod-render.yaml
grep -n "token.place" /tmp/tokenplace-prod-render.yaml
grep -n "tokenplace-prod-tls" /tmp/tokenplace-prod-render.yaml
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://token.place/
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/
```

Optional note: true relay traffic validation requires a registered external compute node and an
E2EE client-flow probe (for example encrypted `/api/v1/chat/completions`).

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

```bash
# Compare local chart metadata with OCI package metadata.
helm show chart ./deploy/charts/tokenplace-relay
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.0

# Render local and OCI manifests, then diff strategy/env sections.
helm template tokenplace ./deploy/charts/tokenplace-relay --namespace tokenplace > /tmp/tokenplace-local.yaml
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.0 --namespace tokenplace > /tmp/tokenplace-oci.yaml
diff -u /tmp/tokenplace-local.yaml /tmp/tokenplace-oci.yaml | less
```

### 2) Missing `Recreate` strategy in deployed chart output

Symptom:
- Deployment renders/rolls out without `spec.strategy.type: Recreate`.

Verification commands:

```bash
grep -n "strategy:" -A4 /tmp/tokenplace-local.yaml
grep -n "strategy:" -A4 /tmp/tokenplace-oci.yaml
kubectl -n tokenplace get deploy tokenplace -o yaml | grep -n "strategy:" -A4
```

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

```bash
kubectl -n tokenplace get deploy tokenplace -o yaml | grep -n "XDG_CONFIG_HOME\\|XDG_CACHE_HOME\\|XDG_DATA_HOME" -A1 -B1
kubectl -n tokenplace logs deploy/tokenplace --tail=200
```

Expected:
- `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, and `XDG_DATA_HOME` are set under container env.
- No read-only filesystem write failure appears in logs after rollout.

### 4) Duplicate environment variable rendering warnings

Symptom:
- Warnings/errors indicate duplicate env entries rendered from both chart defaults and values
  overlays.

Verification commands:

```bash
grep -n "name: TOKENPLACE_" -A1 /tmp/tokenplace-oci.yaml
awk '/env:/{flag=1;next}/imagePullPolicy:/{flag=0}flag' /tmp/tokenplace-oci.yaml | grep "name:" | sort | uniq -d
helm upgrade --install tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.0 --namespace tokenplace --dry-run=client
```

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

```bash
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/relay/diagnostics
curl -fsS https://staging.token.place/metrics | head -n 40
```

### Image tag, chart version, and Git tag must be validated independently

For v0.1.0 launch readiness, verify all three identifiers explicitly:

- Git release tag: `v0.1.0`
- Chart package version: `0.1.0` (with chart `appVersion: "0.1.0"`)
- Relay image tag: `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`

Example image-tag existence check:

```bash
docker manifest inspect ghcr.io/futuroptimist/tokenplace-relay:v0.1.0 >/dev/null
```

### Relay-only health output expectations (staging and production)

For Sugarkube relay-only deployments, healthy relay readiness does **not** require
`TOKENPLACE_RELAY_UPSTREAM_URL` or an in-cluster GPU service. External compute nodes can register
later.

Before (misleading in relay-only mode):

```json
{
  "status": "ok",
  "publicBaseUrl": "https://staging.token.place",
  "knownServers": 0,
  "configuredUpstreamServers": ["https://token.place"],
  "upstream": "http://gpu-server:3000"
}
```

After (explicit relay-only semantics):

```json
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
```

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
