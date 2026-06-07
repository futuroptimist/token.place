# token.place relay on k3s+sugarkube (staging)

> **Environment status:** active validation environment before production promotion.

## Scope

Staging deploys **relay.py only** at default hostname `https://staging.token.place`.
Compute nodes remain external (`server.py`, desktop Tauri nodes, Macs, Windows PCs, Raspberry Pi
GPU/AI hat nodes, and other compute hosts).

No in-cluster backend/GPU service is required in this phase.

## Stateful behavior and replica policy

Relay runtime state (registrations, queued messages, replies) is currently in-memory.
Operational policy for staging is intentionally:

- one pod
- one Gunicorn worker
- one replica

State loss on pod restart/replacement is accepted for now. Shared-state and multi-replica relay
architecture are future work and out of scope.

The canonical token.place Helm chart now defaults to strict single-pod rollout semantics for this
stateful relay phase by rendering `replicaCount: 1` and `strategy.type: Recreate`.

## Artifacts and tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Launch runtime alignment for v0.1.0: Git tag `v0.1.0`, chart `appVersion: "0.1.0"`, release image `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`; updated chart defaults publish as chart package version `0.1.1`
- Preferred tag for validation/sign-off: immutable `main-<shortsha>`
- Final release Git tags publish matching image tags (example: `v0.1.0` -> `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`)
- `main-latest` is convenience-only
- Pre-publish gate: run `helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.1`; if chart `0.1.1` already exists and contents are stale/mismatched, do not overwrite or re-push it; stop and decide manually. If chart `0.1.1` does not exist, proceed with publishing chart package version `0.1.1`.

## Deployment commands (run from Sugarkube repo)

> These commands run from a **Sugarkube checkout**, not from token.place.
>
> Use the values files and version file that live in Sugarkube for your environment; `PATH/TO/*` placeholders below are intentionally repo-local to Sugarkube.
>
## Ingress TLS + Cloudflare Tunnel contract

- Cloudflare Tunnel still owns public DNS/Tunnel routing for `staging.token.place` to Traefik.
- Helm values only control Kubernetes resources; Helm does **not** create/manage Cloudflare routes, DNS, TLS edge policy, Access policy, or WAF/skip rules.
- API v1 compute-node registration, poll, unregister, and encrypted response POST paths can be blocked before reaching `relay.py`; validate Cloudflare Security Events by `cf-ray` when desktop/compute nodes see pre-app 403s.
- Staging values must explicitly set `ingress.tls.enabled: true` or chart output omits `spec.tls`.
- Assumption: cert-manager is installed and `cert-manager.io/cluster-issuer: letsencrypt-production` exists.

Expected staging overlay keys:

```yaml
ingress:
  enabled: true
  className: traefik
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-production
  host: staging.token.place
  tls:
    enabled: true
    secretName: tokenplace-staging-tls
```

First install:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=PATH/TO/tokenplace.values.dev.yaml,PATH/TO/tokenplace.values.staging.yaml version_file=PATH/TO/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=PATH/TO/tokenplace.values.dev.yaml,PATH/TO/tokenplace.values.staging.yaml version_file=PATH/TO/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

## Validation checklist

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

## Promotion/sign-off gate

The validation commands above (`app-status`, `app-verify`, rendered/live YAML checks, `/livez`,
`/healthz`, `/relay/diagnostics`, and `/`) are necessary but insufficient. Do not sign off from
generic HTTP checks alone.

Required gate for `staging.token.place`:

1. Use only the canonical GHCR image and OCI Helm chart path (`ghcr.io/futuroptimist/tokenplace-relay`
   plus `oci://ghcr.io/futuroptimist/charts/tokenplace`), with chart package `0.1.1` and immutable
   image tag `main-REPLACE_SHORTSHA` or another approved immutable tag.
2. Confirm a real external desktop/compute node registers to `staging.token.place` and appears in both `/healthz`
   and `/relay/diagnostics`. The exact compute-node command is operator/environment-specific; use
   the desktop or compute-node runbook for the hardware under test.
3. Complete a real encrypted API v1 relay/desktop-bridge E2EE request/response through that
   registered node. Plaintext relay-dispatched API v1 payloads remain intentionally fail-closed and
   are not production-readiness evidence.
4. Capture evidence after the compute test: immutable image tag, chart version/digest when
   available, `/tmp/tokenplace-staging-render.yaml` or live Deployment YAML, `/healthz`, `/relay/diagnostics`, and relay logs.

Cloudflare/TLS/WAF routing is outside Helm. If compute-node registration sees a non-JSON `403` or
`cf-ray`, check Cloudflare Security Events for that Ray ID before changing the chart or relay code.

If operators use a non-default staging hostname, apply the same checks with that host.

## Rollback

- Record baseline revision: `helm history tokenplace -n tokenplace`
- Roll back release and/or tag per Sugarkube process.
- Re-run validation checks and capture operator notes.

## Notes

- Keep API v1 relay-blind E2EE guardrails intact.
- Do not depend on local chart path deployment (`./deploy/charts/tokenplace-relay`) for
  Sugarkube steady-state operations.
- Do not require `gpuExternalName` or `TOKENPLACE_RELAY_UPSTREAM_URL` for staging relay readiness.
