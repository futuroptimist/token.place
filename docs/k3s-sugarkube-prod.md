# token.place k3s+sugarkube runbook (prod)

> Environment status: **planned target operating model for relay.py; post-parity API v1
> state remains future work**

## Scope

Production focuses on operating `relay.py` reliably on sugarkube with conservative change
controls. Compute nodes remain external until migration phases complete.

## Topology

- In-cluster: highly available `relay.py` deployment behind ingress.
- Out-of-cluster: trusted compute nodes (`server.py` now; desktop-tauri after parity).
- Public path: Cloudflare -> ingress -> relay -> external compute.

## Prerequisites

- Approved production hostname and Cloudflare route.
- Image provenance and pinning policy in place.
- Secrets management process for relay/node auth material.
- Monitoring/alerting for relay health and error rates.

## Release model

- Production deploys only from promoted staging artifacts (pinned tag/digest).
- Use maintenance windows or progressive rollouts per operator policy.

## Deployment steps

1. Confirm artifact promotion metadata from staging.
2. Deploy relay release with immutable image reference.
3. Verify ingress, tunnel, and health checks.
4. Validate external compute node connectivity.
5. Observe metrics/logs before declaring success.

## Validation and SLO checks

- Health endpoints remain green during and after rollout.
- Error rates and latency remain within production thresholds.
- Registration/routing behavior remains stable across node restarts.

## Rollback

- Roll back immediately to last known-good artifact on SLO regression.
- Keep rollback command/procedure scripted and periodically rehearsed.

## Operator notes

- Keep relay lightweight; avoid colocating heavy model runtimes in-cluster unless future
  architecture explicitly changes.
- Maintain explicit boundary: relay operations can mature ahead of API v1 distributed
  compute migration.

## Post-API-v1 target state (future)

After parity and API v1 migration, production should run with all token.place
components aligned on secure API v1 contracts and retired legacy bridging paths.

## Readiness checklist

- [ ] Artifact promotion from staging is enforced.
- [ ] Rollback procedure tested and timed.
- [ ] Monitoring + alerting cover relay health and routing failures.
- [ ] External compute node trust/onboarding policy documented.
