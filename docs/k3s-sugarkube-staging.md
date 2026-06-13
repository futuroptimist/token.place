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
- Chart metadata: chart package version `0.1.2` preserves `appVersion: "0.1.1"`; the historical `v0.1.0` Git tag and `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0` image are immutable release-artifact examples, not a current alignment requirement.
- Preferred tag for validation/sign-off: immutable `main-<shortsha>`
- Final release Git tags publish matching image tags (example: `v0.1.0` -> `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`)
- `main-latest`, `latest`, `staging`, `prod`, and `production` are mutable/convenience labels only and must not be used for staging sign-off or production promotion
- Pre-publish gate: run `helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.2`; if chart `0.1.2` already exists and contents are stale/mismatched, do not overwrite or re-push it; stop and decide manually. If chart `0.1.2` does not exist, proceed with publishing chart package version `0.1.2`.

## Deployment commands (run from Sugarkube repo)

> These commands run from a **Sugarkube checkout**, not from token.place.
>
> Use the values files and version file that live in Sugarkube for your environment; `PATH/TO/*` placeholders below are intentionally repo-local to Sugarkube.
>
## Ingress TLS + Cloudflare Tunnel contract

- Cloudflare Tunnel still owns public DNS/Tunnel routing for `staging.token.place` to Traefik.
- Helm values only control Kubernetes resources; Helm does **not** create/manage Cloudflare routes, DNS, WAF, or Access policies.
- Cloudflare route/TLS/WAF validation is an external release gate because desktop/compute-node registration can be blocked before the request reaches `relay.py`.
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
curl -fsS https://staging.token.place/relay/diagnostics
curl -fsS https://staging.token.place/
```

The generic Sugarkube status, `app-status`, `app-verify`, `/livez`, `/healthz`,
`/relay/diagnostics`, and root HTTP checks are necessary but insufficient for staging sign-off.
Promotion from staging also requires all of the following evidence:

- A real external desktop/compute node registers to `https://staging.token.place` and appears in
  both `/healthz` and `/relay/diagnostics`; the exact compute-node start command is
  operator/environment-specific, so record the command actually used instead of inventing a
  universal one.
- A real encrypted API v1 relay/desktop-bridge E2EE request/response succeeds through that
  registered node. Plaintext relay-dispatched API v1 paths are intentionally fail-closed and are
  not production-readiness evidence.
- Captured evidence includes the immutable image tag, chart version and digest where available,
  rendered or live deployment YAML, `/healthz` and `/relay/diagnostics` output after the compute
  test, and relay logs after the compute test.

See `relay_sugarkube_onboarding.md` for required checks covering stale OCI chart detection,
Recreate strategy verification, XDG read-only-root safeguards, duplicate env detection,
Cloudflare/TLS/WAF gates, and external compute-node E2EE flow validation.

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
