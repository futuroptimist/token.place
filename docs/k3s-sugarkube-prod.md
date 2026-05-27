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
- Required sign-off tag style: immutable semver release tag `vX.Y.Z` (published from a signed-off main artifact).
- Canonical release image tag after Git tagging is the matching semver tag (example: `v0.1.0` -> `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`)
- `main-latest` is convenience-only and not production sign-off

## Pre-flight

Verify the `0.1.0` OCI chart after token.place publishes the current chart:

```bash
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.0
```

If `helm show chart ... --version 0.1.0` succeeds before final token.place chart publish, confirm the discovered chart is not stale before deploying.

## Deployment commands (run from Sugarkube repo)

> Run from a **Sugarkube checkout**, not from token.place.

Use the values files and version file that live in Sugarkube for your environment; `PATH/TO/*` placeholders below are intentionally repo-local to Sugarkube.
>
## Ingress TLS + Cloudflare Tunnel contract

- Cloudflare Tunnel still owns public DNS/Tunnel routing for `token.place` to Traefik.
- Helm values only control Kubernetes resources; Helm does **not** create/manage Cloudflare routes.
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

> Tag selection: use `default_tag=main-<shortsha>` while validating a staging candidate.
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
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version 0.1.0
CHART_VERSION="$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' PATH/TO/tokenplace.version | head -n1)"
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$CHART_VERSION" --namespace tokenplace -f PATH/TO/tokenplace.values.dev.yaml -f PATH/TO/tokenplace.values.prod.yaml --set image.tag=v0.1.0 > /tmp/tokenplace-prod-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-prod-render.yaml
grep -n "token.place" /tmp/tokenplace-prod-render.yaml
grep -n "tokenplace-prod-tls" /tmp/tokenplace-prod-render.yaml
grep -n "type: Recreate" /tmp/tokenplace-prod-render.yaml
yq e 'select(.kind == "Ingress" and .metadata.name == "tokenplace") | .spec.tls' /tmp/tokenplace-prod-render.yaml
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://token.place/
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/
```

Optional note: true relay traffic validation requires a registered external compute node plus an
E2EE client-flow probe; health/root checks alone do not prove register/poll/request/response flow.

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
