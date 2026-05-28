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

## v0.1.0 staging failure modes and fast triage runbook

This section captures failures seen during `v0.1.0` staging so operators can quickly detect and
correct them without changing launch identifiers away from `0.1.0`.

### Critical distinction: image tag vs chart version vs Git tag

- App image tag (example): `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`
- Chart package version: `0.1.0`
- Git tag: `v0.1.0`

All three identifiers can drift independently; verify each explicitly before sign-off.

### Failure modes observed in staging

1. **Stale OCI chart `0.1.0` in GHCR**
   - Symptom: rendered OCI manifest did not include `strategy.type: Recreate` while local chart did.
   - Why it matters: relay is operationally stateful in-memory; non-`Recreate` rollout can overlap pods and lose/duplicate relay state transitions.
2. **Pre-launch `0.1.0` chart delete/re-publish decision**
   - Symptom: GHCR chart `0.1.0` existed but package contents were stale for launch requirements.
   - Decision rule for pre-launch only: if `0.1.0` is stale before launch sign-off, delete stale package and re-publish corrected `0.1.0` so launch artifacts remain aligned at `0.1.0`.
3. **Read-only root filesystem crash in relay container**
   - Symptom: container CrashLoop when runtime attempted writes under default XDG paths on read-only root.
   - Fix: redirect XDG write paths to writable tmpfs locations (`/tmp`).
4. **Duplicate environment warnings**
   - Symptom: Helm/Kubernetes warnings for duplicate env keys when both chart defaults and values overlays set the same variable.
   - Fix: keep each env key defined once in final rendered manifest.
5. **Desktop external compute-node long-poll timeout**
   - Symptom: desktop compute node registration appears successful, but long-poll request/response path times out against staging.
   - Fix approach: validate API v1 register/poll flow against live staging relay, then confirm queued client request is received and replied to by the external compute node.
6. **Health checks green without true relay flow validation**
   - Symptom: `/livez`, `/healthz`, `/`, and `/metrics` all green while end-to-end relay compute path is still broken.
   - Required interpretation: these endpoints are necessary readiness checks, but they are not sufficient for production sign-off.

### Fast verification commands (staging incident playbook)

```bash
# 1) Compare local chart render vs OCI chart render for the same values/tag.
helm template tokenplace ./deploy/charts/tokenplace-relay \
  --namespace tokenplace \
  -f PATH/TO/tokenplace.values.dev.yaml \
  -f PATH/TO/tokenplace.values.staging.yaml \
  --set image.tag=v0.1.0 > /tmp/tokenplace-local-render.yaml

helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace \
  --version 0.1.0 \
  --namespace tokenplace \
  -f PATH/TO/tokenplace.values.dev.yaml \
  -f PATH/TO/tokenplace.values.staging.yaml \
  --set image.tag=v0.1.0 > /tmp/tokenplace-oci-render.yaml

# 2) Confirm Recreate strategy exists in OCI render.
rg -n "strategy:|type: Recreate" /tmp/tokenplace-oci-render.yaml

# 3) Confirm XDG paths are redirected away from read-only root into /tmp.
rg -n "XDG_CONFIG_HOME|XDG_CACHE_HOME|XDG_DATA_HOME|XDG_STATE_HOME|/tmp" /tmp/tokenplace-oci-render.yaml

# 4) Detect duplicate env keys in rendered Deployment env block (should be empty output).
python - <<'PY'
from collections import Counter
from pathlib import Path
import re

text = Path("/tmp/tokenplace-oci-render.yaml").read_text()
env_lines = re.findall(r"^\s*- name:\s*([A-Z0-9_]+)\s*$", text, flags=re.M)
dupes = [k for k, c in Counter(env_lines).items() if c > 1]
print("\n".join(dupes))
PY

# 5) Confirm staging candidate image tag exists in GHCR (example v0.1.0).
docker manifest inspect ghcr.io/futuroptimist/tokenplace-relay:v0.1.0 >/dev/null && echo "image tag exists"
```

### External compute-node validation checklist (required sign-off gate)

Do not promote based only on relay self-health endpoints. For `v0.1.0`, sign-off requires all of:

1. External compute node registers with staging relay.
2. `/healthz` shows `knownServers` incremented from `0` to `>=1`.
3. `/relay/diagnostics` includes the registered compute node identity/route metadata.
4. Client request is accepted and queued on relay (API v1 path).
5. External compute node receives queued request via long-poll.
6. Client successfully retrieves encrypted response produced by the compute node.

If any checklist step fails, do not sign off release readiness even when `/livez`, `/healthz`, `/`,
and `/metrics` are green.

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
