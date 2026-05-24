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
`maxSurge=0` with matching availability constraints) in Sugarkube values before production-like
validation.

## Artifacts and tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Preferred tag for validation/sign-off: immutable `main-<shortsha>`
- `main-latest` is convenience-only

## Deployment commands (run from Sugarkube repo)

> These commands run from a **Sugarkube checkout**, not from token.place.

First install:

```bash
just helm-oci-install release=tokenplace-relay namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace-relay.values.dev.yaml,docs/examples/tokenplace-relay.values.staging.yaml default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace-relay namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace-relay.values.dev.yaml,docs/examples/tokenplace-relay.values.staging.yaml default_tag=main-REPLACE_SHORTSHA
```

Sugarkube-specific tokenplace wrappers may exist after follow-up Sugarkube prompts.

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace-relay --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
# relay traffic smoke test (requires a registered external compute node)
curl -fsS https://staging.token.place/api/v1/models
# optional: run an end-to-end encrypted /api/v1/chat/completions probe via the client flow
# to verify register -> poll -> inference -> response works, not just health endpoints.
```

If operators use a non-default staging hostname, apply the same checks with that host.

## Rollback

- Record baseline revision: `helm history tokenplace-relay -n tokenplace`
- Roll back release and/or tag per Sugarkube process.
- Re-run validation checks and capture operator notes.

## Notes

- Keep API v1 relay-blind E2EE guardrails intact.
- Do not depend on local chart path deployment (`./deploy/charts/tokenplace-relay`) for
  Sugarkube steady-state operations.
- Do not require `gpuExternalName` or `TOKENPLACE_RELAY_UPSTREAM_URL` for staging relay readiness.
