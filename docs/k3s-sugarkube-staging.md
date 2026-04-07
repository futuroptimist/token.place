# token.place relay on k3s+sugarkube (staging)

> **Environment status:** Planned/partial today; target is stable pre-prod relay hosting.
> **Scope:** relay.py only, with external compute nodes on legacy contract.

## Purpose

Staging is the pre-production proving ground for relay upgrades, ingress correctness, and runbook
rehearsal before production rollouts.

## Topology (near-term staging)

- In-cluster: `tokenplace-relay` on sugarkube + ingress route (for example `staging.token.place`).
- Out-of-cluster: `server.py` compute node(s) continue handling inference.
- Contract: legacy sink/source registration + forwarding, not distributed API v1 compute.

## Prerequisites

- Staging namespace/release access in sugarkube cluster.
- Cloudflare tunnel and DNS route for staging hostname.
- GHCR image pull access.
- External compute node endpoint and optional registration token.

## Release model

- Promote immutable image tags from dev.
- Maintain chart values parity with production where practical.
- Track every staging deployment with image tag, chart version, and rollout timestamp.

## Deploy (template)

```bash
helm upgrade --install tokenplace-relay ./deploy/charts/tokenplace-relay \
  --namespace tokenplace-staging --create-namespace \
  --set image.repository=ghcr.io/democratizedspace/tokenplace-relay \
  --set image.tag=<immutable-tag> \
  --set ingress.enabled=true \
  --set ingress.className=traefik \
  --set ingress.hosts[0].host=staging.token.place \
  --set upstream.url=<external-compute-url>
```

If sugarkube `just` wrappers become canonical for token.place, replace this command with the exact
wrapper invocation and keep this section up to date.

## Validation checklist

- [ ] Ingress route and Cloudflare tunnel resolve correctly.
- [ ] `/healthz` is healthy via `https://staging.token.place/healthz`.
- [ ] Relay forwards requests to external compute node(s).
- [ ] Rollback drill succeeds with previous immutable tag.

## Rollback

- `helm rollback` to previous release revision or redeploy last known-good immutable tag.
- Validate health endpoint and forwarding behavior.
- Log incident notes and promotion block reason if rollback was required.

## Operator notes

- Staging should mirror production security posture where possible.
- Keep runtime parity and API migration state visible in release notes to avoid overclaiming.
- Continue treating distributed API v1 compute as post-parity work.

## Forward state (not current)

Post-parity and post-API-v1 migration, staging will validate API v1 distributed-compute topology
before production cutovers.

See also:
- [relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md)
- [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
