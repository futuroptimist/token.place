# token.place relay.py on sugarkube onboarding

## Purpose and current status

`relay.py` is lightweight, stateless enough for horizontal rollout, and a better fit for
sugarkube than colocating compute-heavy workloads in-cluster. That makes relay-first
sugarkube onboarding a near-term operational step even before desktop/server parity and
before API v1 distributed migration.

- **Current state:** relay can run locally and in k3s patterns; compute nodes remain
  external (`server.py` today, desktop-tauri later).
- **Near-term goal:** standardize relay deployment on sugarkube dev/staging/prod.
- **Target state (later):** relay + compute components align to post-parity API v1
  distributed model.

## Why relay.py belongs on sugarkube

- Small operational footprint compared with model-serving workloads.
- Benefits from centralized ingress, health checks, rollout controls, and observability.
- Lets operators harden a shared front door while compute nodes evolve independently.

## Minimal requirements

- A reachable k3s/sugarkube cluster namespace for token.place.
- A deployable relay image (`ghcr.io/democratizedspace/tokenplace-relay`) and chart or
  manifest path.
- Ingress path for public hostnames where required.
- External network reachability from relay pods to compute nodes.
- Runtime configuration for relay upstream URL and optional registration token policy.

## Networking, ingress, and Cloudflare tunnel expectations

Expected path for hosted environments:

`Client -> Cloudflare (DNS/tunnel) -> Traefik ingress -> relay.py service -> external compute node`

Operator expectations:

- Keep relay externally reachable only through intended ingress hostname(s).
- Keep compute node endpoints private/restricted; relay egress should be scoped.
- Prefer Cloudflare proxied hostnames for internet-exposed dev/staging/prod endpoints.

## Config and secrets expectations

Baseline configuration surface (names depend on deployment wrapper):

- Relay listen host/port.
- Public relay URL (for diagnostics and client defaults).
- Upstream compute URL(s) for legacy relay contract.
- Optional node registration token/secrets for authenticated node participation.

Secret handling guidance:

- Store registration tokens in Kubernetes secrets, never committed plaintext.
- Rotate tokens when onboarding/offboarding nodes.
- Avoid logging plaintext request/response content.

## Health checks and validation

Minimum runtime checks:

- Liveness probe (`/livez`) and readiness probe (`/healthz`) return expected status.
- Ingress hostname resolves and reaches relay service.
- Relay can reach at least one external compute node.
- Synthetic request passes through relay path without plaintext exposure in logs.

Suggested validation commands (adapt namespace/release names):

```bash
kubectl -n tokenplace get pods
kubectl -n tokenplace get ingress
curl -fsS https://<env-hostname>/healthz
```

## Current limitations (explicit)

- Desktop-tauri is still MVP and not yet a full compute-node replacement.
- Legacy `/sink` + `/source` contract remains the active distributed path today.
- API v1 distributed compute migration is planned but not completed.
- Some sugarkube automation command names may remain TODO until chart/just wiring is
  finalized in deployment repos.

## How this fits the roadmap

This onboarding document corresponds to **Step 5** in the canonical migration sequence,
which intentionally happens after desktop parity work has started but before post-parity
API v1 migration is complete.

- Canonical roadmap: [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
- Desktop design: [design/tauri_desktop_client.md](design/tauri_desktop_client.md)

## Environment runbooks

- Dev: [k3s-sugarkube-dev.md](k3s-sugarkube-dev.md)
- Staging: [k3s-sugarkube-staging.md](k3s-sugarkube-staging.md)
- Prod: [k3s-sugarkube-prod.md](k3s-sugarkube-prod.md)
