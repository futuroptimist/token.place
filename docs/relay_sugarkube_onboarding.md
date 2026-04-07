# token.place relay-on-sugarkube onboarding

## Why relay.py belongs on sugarkube

`relay.py` is a lightweight HTTP forwarding service with small resource requirements and a clean
operational boundary. Moving relay onto sugarkube earlier gives us:

- stable public ingress managed by cluster operators,
- simplified endpoint management for external compute nodes,
- safer separation between cluster operations and heavyweight compute-node runtimes.

This is intentionally a **relay-first** move. Compute nodes (`server.py` today, desktop nodes later)
remain external during near-term phases.

## Status framing

- **Current state:** relay can run locally or in ad hoc deployments; compute remains external.
- **Near-term target:** relay is deployed on sugarkube (dev/staging/prod) with consistent runbooks.
- **Target state (post-parity, post-API-v1 migration):** relay and compute components are aligned on
  API v1 distributed contracts with secure auth/routing.

## Minimal requirements

- k3s cluster with ingress controller (Traefik assumed in existing docs).
- Container pull access for relay image (`ghcr.io/democratizedspace/tokenplace-relay`).
- Namespace and release process for `tokenplace-relay` Helm chart.
- External compute node reachable from cluster egress (until distributed migration phases).

## Networking, ingress, and Cloudflare tunnel expectations

- Public hostnames should be routed through Cloudflare tunnel to Traefik.
- Typical path: `client -> Cloudflare -> tunnel -> Traefik ingress -> relay service`.
- Relay upstream to compute remains outbound from cluster to external compute node endpoint.
- Environment-specific hostnames are tracked in the dev/staging/prod runbooks.

## Health checks and validation guidance

Minimum operator checks after each deploy:

1. Pod and rollout health (`kubectl get pods`, `kubectl rollout status`).
2. Ingress objects and hostname mapping (`kubectl get ingress`).
3. Relay health endpoint (`GET /healthz`).
4. Basic relay request path smoke test from outside cluster.
5. External compute registration/forwarding verification in logs.

## Secrets and configuration expectations

At minimum, define:

- relay upstream URL and port values,
- optional relay/server registration token,
- environment-specific public URL metadata for diagnostics.

Store credentials in cluster-managed secret objects. Do not commit secrets into repo values files.

## Current limitations

- This onboarding does not imply compute-node migration into cluster.
- It does not claim `desktop-tauri/` parity or replacement of `server.py`.
- It does not claim API v1 distributed compute is already implemented.

## Roadmap fit

Relay-on-sugarkube is the operations track aligned to steps 5-6 of the canonical roadmap and can
progress while runtime parity work continues.

- Canonical roadmap: [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
- Desktop strategy: [design/tauri_desktop_client.md](design/tauri_desktop_client.md)

## Environment runbooks

- Dev: [k3s-sugarkube-dev.md](k3s-sugarkube-dev.md)
- Staging: [k3s-sugarkube-staging.md](k3s-sugarkube-staging.md)
- Prod: [k3s-sugarkube-prod.md](k3s-sugarkube-prod.md)
