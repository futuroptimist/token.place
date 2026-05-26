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

Kubernetes `Deployment` upgrades may briefly run more than one pod under the default
`RollingUpdate` strategy (`maxSurge`). To preserve strict single-pod semantics for stateful relay
operations, operators should configure a single-pod rollout policy (for example `Recreate` or
`maxSurge=0` with matching availability constraints) in Sugarkube values.

## Artifacts and tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Preferred tag for validation/sign-off: immutable `main-<shortsha>`
- `main-latest` is convenience-only

## Deployment commands (run from Sugarkube repo)

> These commands run from a **Sugarkube checkout**, not from token.place.
>
> `docs/examples/tokenplace.values.*.yaml` and `docs/apps/tokenplace.version` are the
> Sugarkube contract artifacts consumed by these commands.

First install:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

## Validation checklist

```bash
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' docs/apps/tokenplace.version | head -n1)" --namespace tokenplace -f docs/examples/tokenplace.values.dev.yaml -f docs/examples/tokenplace.values.staging.yaml --set image.tag=main-REPLACE_SHORTSHA > /tmp/tokenplace-staging-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-staging-render.yaml
grep -n "staging.token.place" /tmp/tokenplace-staging-render.yaml
grep -n "tokenplace-staging-tls" /tmp/tokenplace-staging-render.yaml
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://staging.token.place/livez
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://staging.token.place/healthz
kubectl -n tokenplace get ingress tokenplace -o yaml
curl -vI https://staging.token.place/
```

Optional note: true relay traffic validation requires a registered external compute node plus an
E2EE client-flow probe; health/root checks alone do not prove register/poll/request/response flow.

The rendered Ingress should include `spec.tls[0].hosts[0]=staging.token.place` and `spec.tls[0].secretName=tokenplace-staging-tls` before deploy.

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
