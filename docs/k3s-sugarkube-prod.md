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

First install:

```bash
just helm-oci-install release=tokenplace-relay namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace-relay.values.dev.yaml,docs/examples/tokenplace-relay.values.prod.yaml default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace-relay namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace-relay.values.dev.yaml,docs/examples/tokenplace-relay.values.prod.yaml default_tag=main-REPLACE_SHORTSHA
```

Sugarkube-specific tokenplace wrapper commands may exist after follow-up Sugarkube work.

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace-relay --timeout=180s
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/
# relay traffic smoke test (requires at least one healthy external compute node)
curl -fsS https://token.place/api/v1/models
# optional: run an end-to-end encrypted /api/v1/chat/completions probe through the
# standard client flow to validate real relay request handling.
```

If operators override hostname/routing, use the equivalent production host in the same checks.

## Rollback

- Record baseline revision: `helm history tokenplace-relay -n tokenplace`
- Roll back to prior approved revision/tag.
- Re-run validation and document outcome.

## Notes

- Preserve API v1 relay-blind E2EE guardrails (relay sees ciphertext + routing metadata only).
- Do not rely on local chart path deployment (`./deploy/charts/tokenplace-relay`) for
  Sugarkube steady-state operations.
- Do not require `gpuExternalName` or `TOKENPLACE_RELAY_UPSTREAM_URL` for production relay
  readiness in this relay-only phase.
