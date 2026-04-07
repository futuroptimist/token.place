# Relay on sugarkube onboarding

This guide explains why and how to run `relay.py` on sugarkube before the desktop/API-v1 migration
is complete.

Canonical roadmap: [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md).

## Status

- **Current state:** relay can run locally or in cluster-style deployments.
- **Near-term plan:** move relay operations to sugarkube first.
- **Not true yet:** full distributed compute API v1 migration.

## Why `relay.py` belongs on sugarkube

- Relay is lightweight and stateless enough for k3s scheduling.
- Sugarkube gives repeatable ingress, rollout, and health-check operations.
- We can improve reliability now without forcing compute-node migration immediately.

## Minimal requirements

- k3s cluster with ingress controller (Traefik in current ops assumptions).
- Pull access to the relay OCI image.
- DNS + edge routing for environment hostnames.
- Secret/config management for relay tokens and upstream URLs.

## Networking and ingress expectations

Typical path:

`Client -> Cloudflare -> Cloudflare Tunnel -> Traefik ingress -> relay.py service`

Notes:

- Keep public hostnames environment-specific (`dev`, `staging`, `prod`).
- Use proxied DNS/edge routing; do not expose ad-hoc node ports publicly.
- Compute nodes remain external to sugarkube during near-term phases.

## Health checks and validation

Relay health probes:

- `GET /livez` for process liveness
- `GET /healthz` for readiness/draining behavior

Minimum validation after rollout:

1. ingress route resolves and returns expected relay status,
2. pod readiness is green,
3. external compute node can still register and exchange legacy sink/source traffic.

## Secrets and configuration expectations

At minimum, operators should define:

- relay public URL for diagnostics/client defaults,
- upstream compute-node URL(s),
- optional relay/server registration token,
- environment identity (`dev`, `staging`, `prod`).

Where exact chart keys are still evolving, treat them as implementation follow-ups and avoid
hard-coding commands in this doc.

## Current limitations

- Desktop is not parity-complete with `server.py` yet.
- API v1 distributed compute migration is not complete yet.
- Mixed-fleet behavior must continue to honor legacy relay contracts until post-parity phases.

## How this fits the roadmap

Relay-on-sugarkube is a near-term operational milestone that can be completed before API v1
compute migration. It corresponds to Step 6 in the canonical 7-step sequence.

## Environment runbooks

- [k3s-sugarkube-dev.md](k3s-sugarkube-dev.md)
- [k3s-sugarkube-staging.md](k3s-sugarkube-staging.md)
- [k3s-sugarkube-prod.md](k3s-sugarkube-prod.md)
