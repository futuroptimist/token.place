# token.place k3s+sugarkube runbook (prod)

## Environment status

- **Environment:** prod
- **Lifecycle state:** planned for relay-first production operations
- **Scope (current plan):** `relay.py` on sugarkube; compute nodes remain external until later
  migration phases are complete.

Related docs:

- Canonical roadmap: [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
- Relay onboarding: [relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md)

## Topology

- Public `token.place` traffic reaches Cloudflare edge/tunnel.
- Traefik ingress routes to sugarkube relay deployment.
- Relay forwards to approved external compute nodes.

## Prerequisites

- Production cluster readiness sign-off.
- Production DNS/Cloudflare route and certificate posture validated.
- Immutable relay artifact promotion policy in place.
- Runbook-tested rollback path and on-call ownership defined.

## Release model

- Production uses immutable image references only.
- Promote from staging after validation evidence is captured.
- Maintain changelog entry with artifact version, deploy time, and rollback target.

> Placeholder: concrete deployment wrapper commands will be documented once tooling stabilizes.
> Until then, do not improvise ad-hoc prod command sequences.

## Validation checklist (prod)

- [ ] Ingress and health endpoints pass from external vantage point.
- [ ] Relay serves traffic without exposing plaintext content.
- [ ] External compute-node connectivity is stable under expected load envelope.
- [ ] Alerting/on-call dashboards confirm healthy steady state.

## Rollback

- Roll back to last known-good immutable revision.
- Validate health checks and traffic restoration.
- Document incident timeline and corrective actions.

## Operator notes

- Relay remains intentionally lightweight on sugarkube.
- Compute remains external by design in this phase.
- Avoid coupling production relay rollout to unfinished desktop/API-v1 migrations.

## Post-API-v1 target note

After post-parity API v1 migration, production should enforce API v1-aligned distributed compute
contracts across relay and compute nodes. This is target state, not current state.
