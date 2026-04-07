# token.place k3s+sugarkube runbook (staging)

## Environment status

- **Environment:** staging
- **Lifecycle state:** near-term planned / pre-production validation
- **Scope:** relay-first deployment (`relay.py` on sugarkube, compute nodes external)

Related docs:

- Canonical roadmap: [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
- Relay onboarding: [relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md)

## Topology

- Internet traffic enters through Cloudflare + tunnel/edge routing.
- Traefik ingress forwards to relay service in sugarkube.
- Relay connects to external compute nodes via configured upstreams.

## Prerequisites

- Staging namespace and ingress class available.
- Staging hostname + DNS/Cloudflare route configured.
- Registry credentials for relay image pulls.
- Relay config/secrets stored in staging secret management path.

## Release model

- Prefer immutable image references in staging.
- Promote only versions that passed dev checklist.
- Record deployed image/tag + config revision in release notes.

> Placeholder: final `helm`/`just` command set is tracked as implementation work. Do not invent
> commands here before repo tooling is finalized.

## Validation checklist (staging)

- [ ] Health endpoints green through ingress.
- [ ] Registration and request routing work with external compute nodes.
- [ ] Restart/failover scenarios do not break client access.
- [ ] Observability is sufficient for pre-production debugging.

## Rollback

- Redeploy previous immutable revision.
- Reconcile ingress/secret drift if rollout changed them.
- Re-run validation checklist before declaring rollback complete.

## Operator notes

- Staging should mirror prod topology where practical.
- Keep external compute assumptions explicit until migration phases complete.
- Confirm no doc drift vs canonical roadmap before each promotion.

## Post-API-v1 target note

Staging will later validate API v1 distributed compute behavior. That phase is post-parity and not
assumed to be complete in this runbook.
