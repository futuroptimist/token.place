# Relay on sugarkube onboarding (token.place)

This guide explains how and why to run `relay.py` on sugarkube before full desktop/server
migration is complete.

## Why relay.py belongs on sugarkube

`relay.py` is lightweight compared with compute nodes and is operationally a good fit for k3s:

- stateless request relay behavior
- modest CPU/memory footprint
- clear health endpoint (`/healthz`)
- easier centralized ingress/tunnel management

This allows token.place to improve relay availability and operator workflows while GPU-heavy
compute nodes (`server.py` and later desktop compute nodes) remain external during parity phases.

Roadmap alignment: [desktop compute-node migration roadmap](roadmap/desktop_compute_node_migration.md).

## Current status

- **Current:** relay can run locally or in Kubernetes; staging-oriented docs exist.
- **Planned near-term:** standardized dev/staging/prod sugarkube runbooks for relay.
- **Not yet:** full post-API-v1 distributed deployment model.

## Minimal requirements

- k3s/sugarkube cluster access (`kubectl`, `helm`)
- container image access for relay
- DNS + Cloudflare tunnel route for chosen hostname
- upstream compute node endpoint(s) reachable from relay route consumers

## Networking expectations

Expected edge path:

`Client -> Cloudflare -> Tunnel -> Traefik Ingress -> relay Service -> relay Pod`

Notes:

- relay remains HTTP inside cluster unless platform policy requires in-cluster TLS.
- public hostnames should be environment-specific (dev/staging/prod).
- allow overriding relay URL in desktop/server configs during phased rollout.

## Secrets and config expectations

At minimum, plan for:

- relay registration/auth token material (if enabled)
- environment-scoped relay public URL and host settings
- any upstream allowlists or routing config

If exact secret names or chart keys are unsettled, treat them as explicit follow-up tasks rather
than inventing values.

## Health checks and validation

Minimum operator checks after deploy:

1. Pod readiness and restart counts are stable.
2. Ingress host resolves and serves `/livez` (process liveness).
3. Ingress host resolves and serves `/healthz` (readiness).
3. Relay can accept registration/polling traffic from external compute nodes.

`relay.py` probe semantics:

- `GET /livez` => process is alive (`200` with `{"status":"alive"}`).
- `GET /healthz` => readiness for traffic:
  - `200` when ready,
  - `503` with `status=draining` during termination,
  - `503` with `status=degraded` when configured GPU host cannot be resolved.

Example checks:

```bash
kubectl -n tokenplace get pods
kubectl -n tokenplace get ingress
curl -fsS https://<env-host>/livez
curl -fsS https://<env-host>/healthz
```

Environment/config keys to set explicitly in deployment values/manifests:

- `TOKENPLACE_RELAY_PUBLIC_URL` (or `TOKEN_PLACE_RELAY_PUBLIC_URL`) for externally advertised base URL.
- `TOKENPLACE_RELAY_UPSTREAM_URL` (or `TOKENPLACE_GPU_HOST`/`TOKENPLACE_GPU_PORT`) for upstream hints.
- `TOKEN_PLACE_RELAY_SERVER_TOKENS` / `TOKEN_PLACE_RELAY_SERVER_TOKEN` for compute-node registration auth.

## Current limitations

- Compute nodes are still external during parity phases.
- Legacy sink/source contract remains active until post-parity API v1 migration.
- Final sugarkube automation wrappers may still be in progress per environment.

## How this fits the broader roadmap

- Relay-on-sugarkube is a **pre-API-v1** operational improvement.
- Desktop parity and shared compute runtime come first for compute-plane migration.
- API v1 distributed compute is a later phase once parity and relay ops are stable.

## Environment runbooks

- [k3s-sugarkube-dev.md](k3s-sugarkube-dev.md)
- [k3s-sugarkube-staging.md](k3s-sugarkube-staging.md)
- [k3s-sugarkube-prod.md](k3s-sugarkube-prod.md)

Each runbook includes rollout + rollback validation; keep those steps tied to `/livez` + `/healthz`
checks and a canary registration poll from at least one external compute node.
