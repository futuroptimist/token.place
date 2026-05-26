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

Kubernetes `Deployment` upgrades may temporarily run more than one pod with default
`RollingUpdate` behavior (`maxSurge`). If strict single-pod relay operation is required during
upgrade windows, set an explicit single-pod rollout strategy (for example `Recreate` or
`maxSurge=0` with compatible availability settings) in Sugarkube values.

## Artifacts and release tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Required sign-off tag style: immutable `main-<shortsha>`
- `main-latest` is convenience-only and not production sign-off

## Deployment commands (run from Sugarkube repo)

> Run from a **Sugarkube checkout**, not from token.place.
>
## Ingress TLS + Cloudflare Tunnel contract

- Cloudflare Tunnel still owns public DNS/Tunnel routing for `token.place` to Traefik.
- Helm values only control Kubernetes resources; Helm does **not** create/manage Cloudflare routes.
- Production values must explicitly set `ingress.tls.enabled: true` or chart output omits `spec.tls`.
- Assumption: cert-manager is installed and `cert-manager.io/cluster-issuer: letsencrypt-prod` exists.

Expected production overlay keys:

```yaml
ingress:
  enabled: true
  className: traefik
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  host: token.place
  tls:
    enabled: true
    secretName: tokenplace-prod-tls
```

First install:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.prod.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.prod.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' docs/apps/tokenplace.version | head -n1)" --namespace tokenplace -f docs/examples/tokenplace.values.dev.yaml -f docs/examples/tokenplace.values.prod.yaml --set image.tag=main-REPLACE_SHORTSHA > /tmp/tokenplace-prod-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-prod-render.yaml
grep -n "token.place" /tmp/tokenplace-prod-render.yaml
grep -n "tokenplace-prod-tls" /tmp/tokenplace-prod-render.yaml
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
