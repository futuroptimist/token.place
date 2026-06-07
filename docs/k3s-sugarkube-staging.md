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
- Current deployable chart package version: `0.1.1` (the v0.1.0 runtime remains chart `appVersion: "0.1.0"`)
- Launch runtime alignment for v0.1.0: Git tag `v0.1.0`, chart `appVersion: "0.1.0"`, release image `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`; deploy with chart package version `0.1.1`
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
- Helm values only control Kubernetes resources; Helm does **not** create/manage Cloudflare routes, DNS, Tunnel ingress rules, TLS edge policy, or WAF/bot rules.
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

Optional note: true relay traffic validation requires a registered external compute node plus an
E2EE client-flow probe; health/root checks alone do not prove register/poll/request/response flow.
See `relay_sugarkube_onboarding.md` "v0.1.0 staging failure modes and operator runbook" for
required checks covering stale OCI chart detection, Recreate strategy verification, XDG
read-only-root safeguards, duplicate env detection, and external compute-node long-poll flow
validation.

## External relay-compute E2EE sign-off gate

The HTTP checklist above is necessary but **not sufficient**. Sign-off also requires a real external
desktop/compute node to register against `https://staging.token.place` and complete one encrypted API v1
relay/desktop-bridge E2EE request/response through that node. The compute-node command is
operator/environment-specific; capture the actual command and redacted config used rather than
inventing a generic one.

Evidence to archive after the compute test:

- immutable image tag (`main-REPLACE_SHORTSHA` or the approved replacement)
- chart version `0.1.1` and chart digest when available from Helm/Sugarkube output
- rendered manifest or live Deployment YAML showing one replica and one worker
- `/healthz` and `/relay/diagnostics` output showing the registered external node after the test
- relay logs captured after the encrypted request/response completes

Cloudflare/TLS/WAF validation is an external gate because Helm cannot prove public routing reaches
Traefik or that compute-node POSTs are allowed through Cloudflare:

```bash
# Confirm Cloudflare/DNS routes the public hostname to the expected edge/Tunnel path.
dig +short staging.token.place
curl -vI https://staging.token.place/
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/relay/diagnostics

# Safely reproduce compute-node registration shape with a placeholder token and redacted body.
# A 401 JSON response means the request reached relay.py but the placeholder token was invalid;
# a 403 with server: cloudflare or cf-ray usually means Cloudflare/WAF blocked it before the app.
curl -i -X POST https://staging.token.place/api/v1/relay/servers/register \
  -H 'content-type: application/json' \
  -H 'X-Relay-Server-Token: REPLACE_WITH_ENVIRONMENT_TEST_TOKEN' \
  --data '{"server_public_key":"redacted-placeholder-public-key"}'

# If a desktop/compute node records a pre-app 403, look up the Ray ID in Cloudflare Security Events.
CF_RAY=REPLACE_CF_RAY
printf 'Cloudflare Security > Events: filter Ray ID %s for host staging.token.place and review WAF/bot/firewall action\n' "$CF_RAY"
```

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
