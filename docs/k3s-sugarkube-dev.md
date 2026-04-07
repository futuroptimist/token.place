# token.place k3s+sugarkube runbook (dev)

## Environment status

- **Environment:** dev
- **Lifecycle state:** current/active for iterative validation
- **Scope:** relay-first deployment (`relay.py` on sugarkube, compute nodes external)

Related docs:

- Canonical roadmap: [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
- Relay onboarding: [relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md)

## Topology

- Public dev hostname routes to sugarkube ingress.
- sugarkube runs relay deployment/service/ingress.
- compute nodes (`server.py` today, desktop nodes later) run outside cluster.

## Prerequisites

- Access to dev cluster context and namespace.
- Access to relay image registry.
- Hostname + Cloudflare route for the dev endpoint.
- Known upstream compute endpoint(s) for relay configuration.

## Release model

Use mutable tags only for quick smoke tests in dev. Prefer moving to immutable tags before
promotion to staging/prod.

> Placeholder: use the repo's selected deployment wrapper (`helm`/`just`) once finalized.
> If wrapper commands change, update this runbook and the roadmap links in the same PR.

## Validation checklist (dev)

- [ ] Relay pod comes up healthy (`/livez`, `/healthz`).
- [ ] Dev hostname reaches relay ingress.
- [ ] External compute node registers and handles legacy flow through relay.
- [ ] Basic fail/restart behavior is acceptable for developer use.

## Rollback

- Roll back to previous known image tag/chart revision.
- If ingress changes caused impact, restore prior ingress config.
- Confirm health endpoints and external node registration after rollback.

## Operator notes

- Dev is where chart keys/commands may still evolve; keep placeholders explicit.
- Do not treat dev success as proof of production readiness.
- Keep compute outside sugarkube until parity and migration gates are met.

## Post-API-v1 target note

After parity + API v1 migration, dev should validate API v1 distributed alignment of relay and
compute nodes. That state is planned, not current.
