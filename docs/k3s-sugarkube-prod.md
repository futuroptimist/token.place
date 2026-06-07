# token.place relay on k3s+sugarkube (prod)

> **Environment status:** production runbook for promoted staging artifacts.

## Scope

Production Sugarkube deployment is **relay.py only** at default hostname `https://token.place`.
Compute plane remains external (`server.py`, desktop Tauri compute nodes, Macs, Windows PCs,
Raspberry Pi GPU/AI hat nodes, and other compute hosts).

No in-cluster backend/GPU service is required for this phase.

## Stateful behavior and replica policy

Relay state is in-memory (registrations, queued messages, replies). Current production policy:

- one pod
- one Gunicorn worker
- one replica

State loss on pod death/replacement is an accepted risk for this phase. Durable/shared state and
multi-replica relay topology are future work.

The canonical token.place Helm chart now defaults to strict single-pod rollout behavior for this
stateful relay phase by rendering `replicaCount: 1` and `strategy.type: Recreate`.

## Artifacts and release tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Current deployable chart package version: `0.1.1` (the v0.1.0 runtime remains chart `appVersion: "0.1.0"`)
- Launch runtime alignment for v0.1.0: Git tag `v0.1.0`, chart `appVersion: "0.1.0"`, release image `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`; deploy with chart package version `0.1.1`
- Required sign-off tag style: immutable semver release tag `vX.Y.Z` (published from a signed-off main artifact)
- Canonical release image tag after Git tagging is the matching semver tag (example: `v0.1.0` -> `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`)
- `main-latest` is convenience-only and not production sign-off
- Pre-publish gate: run `helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.1`; if chart `0.1.1` already exists and contents are stale/mismatched, do not overwrite or re-push it; stop and decide manually. If chart `0.1.1` does not exist, proceed with publishing chart package version `0.1.1`.

## Deployment commands (run from Sugarkube repo)

> Run from a **Sugarkube checkout**, not from token.place.

Use the values files and version file that live in Sugarkube for your environment; `PATH/TO/*` placeholders below are intentionally repo-local to Sugarkube.
>
## Ingress TLS + Cloudflare Tunnel contract

- Cloudflare Tunnel still owns public DNS/Tunnel routing for `token.place` to Traefik.
- Helm values only control Kubernetes resources; Helm does **not** create/manage Cloudflare routes, DNS, Tunnel ingress rules, TLS edge policy, or WAF/bot rules.
- Production values must explicitly set `ingress.tls.enabled: true` or chart output omits `spec.tls`.
- Assumption: cert-manager is installed and `cert-manager.io/cluster-issuer: letsencrypt-production` exists.

Expected production overlay keys:

```yaml
ingress:
  enabled: true
  className: traefik
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-production
  host: token.place
  tls:
    enabled: true
    secretName: tokenplace-prod-tls
```

> Tag selection: use `default_tag=main-REPLACE_SHORTSHA` while validating a staging candidate.
> After pushing the real Git release tag (for example `v0.1.0`), use `default_tag=v0.1.0` as the
> canonical production release image tag.

First install:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=PATH/TO/tokenplace.values.dev.yaml,PATH/TO/tokenplace.values.prod.yaml version_file=PATH/TO/tokenplace.version default_tag=v0.1.0
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=PATH/TO/tokenplace.values.dev.yaml,PATH/TO/tokenplace.values.prod.yaml version_file=PATH/TO/tokenplace.version default_tag=v0.1.0
```

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
CHART_VERSION="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' PATH/TO/tokenplace.version | head -n1)"
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$CHART_VERSION" --namespace tokenplace -f PATH/TO/tokenplace.values.dev.yaml -f PATH/TO/tokenplace.values.prod.yaml --set image.tag=v0.1.0 > /tmp/tokenplace-prod-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-prod-render.yaml
grep -n "token.place" /tmp/tokenplace-prod-render.yaml
grep -n "tokenplace-prod-tls" /tmp/tokenplace-prod-render.yaml
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://token.place/
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/relay/diagnostics
curl -fsS https://token.place/
```

Optional note: true relay traffic validation requires a registered external compute node plus an
E2EE client-flow probe; health/root checks alone do not prove register/poll/request/response flow.
See `relay_sugarkube_onboarding.md` "v0.1.0 staging failure modes and operator runbook" and run
the same external compute-node validation pattern against production hostnames before sign-off.

## External relay-compute E2EE sign-off gate

The HTTP checklist above is necessary but **not sufficient**. Sign-off also requires a real external
desktop/compute node to register against `https://token.place` and complete one encrypted API v1
relay/desktop-bridge E2EE request/response through that node. The compute-node command is
operator/environment-specific; capture the actual command and redacted config used rather than
inventing a generic one.

Evidence to archive after the compute test:

- immutable image tag (`v0.1.0` or the approved replacement)
- chart version `0.1.1` and chart digest when available from Helm/Sugarkube output
- rendered manifest or live Deployment YAML showing one replica and one worker
- `/healthz` and `/relay/diagnostics` output showing the registered external node after the test
- relay logs captured after the encrypted request/response completes

Cloudflare/TLS/WAF validation is an external gate because Helm cannot prove public routing reaches
Traefik or that compute-node POSTs are allowed through Cloudflare:

```bash
# Confirm Cloudflare/DNS routes the public hostname to the expected edge/Tunnel path.
dig +short token.place
curl -vI https://token.place/
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/relay/diagnostics

# Safely reproduce compute-node registration shape with a placeholder token and redacted body.
# A 401 JSON response means the request reached relay.py but the placeholder token was invalid;
# a 403 with server: cloudflare or cf-ray usually means Cloudflare/WAF blocked it before the app.
curl -i -X POST https://token.place/api/v1/relay/servers/register \
  -H 'content-type: application/json' \
  -H 'X-Relay-Server-Token: REPLACE_WITH_ENVIRONMENT_TEST_TOKEN' \
  --data '{"server_public_key":"redacted-placeholder-public-key"}'

# If a desktop/compute node records a pre-app 403, look up the Ray ID in Cloudflare Security Events.
CF_RAY=REPLACE_CF_RAY
printf 'Cloudflare Security > Events: filter Ray ID %s for host token.place and review WAF/bot/firewall action\n' "$CF_RAY"
```

If operators override hostname/routing, use the equivalent production host in the same checks.

## Rollback

- Record baseline revision: `helm history tokenplace -n tokenplace`
- Roll back to prior approved revision/tag.
- Re-run validation and document outcome.

## Notes

- Preserve API v1 relay-blind E2EE guardrails (relay sees ciphertext + routing metadata only).
- Do not rely on local chart path deployment (`./deploy/charts/tokenplace-relay`) for
  Sugarkube steady-state operations.
- Do not require `gpuExternalName` or `TOKENPLACE_RELAY_UPSTREAM_URL` for production relay
  readiness in this relay-only phase.
