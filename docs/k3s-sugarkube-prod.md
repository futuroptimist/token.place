# token.place relay on k3s+sugarkube (prod)

> **Environment status:** Planned target for steady-state relay hosting; rollout gated by staging
> evidence.
> **Scope:** production relay.py service, external compute nodes retained until later migration
> phases.

## Purpose

Provide resilient production ingress and routing for token.place relay while preserving current
compute-node operations.

## Topology (production target before API-v1 migration)

- In-cluster: highly available relay deployment on sugarkube.
- Edge: Cloudflare tunnel + DNS hostname (for example `token.place`).
- Out-of-cluster compute: `server.py` and later parity-ready desktop nodes.
- Contract: legacy sink/source + multi-node registration until post-parity API migration.

## Prerequisites

- Production namespace and RBAC in sugarkube.
- Approved immutable image tag promoted from staging.
- Cloudflare production route and incident-owner rotation.
- Secrets present (registration token and any upstream auth configuration).

## Release model

- Production uses immutable image tags only.
- Promotion path: dev -> staging -> prod.
- Every prod rollout must include validation and rollback checkpoints.

## Deploy (template)

```bash
helm upgrade --install tokenplace-relay ./deploy/charts/tokenplace-relay \
  --namespace tokenplace-prod --create-namespace \
  --set image.repository=ghcr.io/democratizedspace/tokenplace-relay \
  --set image.tag=<approved-immutable-tag> \
  --set ingress.enabled=true \
  --set ingress.className=traefik \
  --set ingress.hosts[0].host=token.place \
  --set upstream.url=<external-compute-url>
```

If a sugarkube-native deploy command becomes canonical, update this runbook to that exact command.

## Validation checklist

- [ ] Rollout succeeds with no crash-looping pods.
- [ ] Public `/healthz` endpoint remains healthy during and after rollout.
- [ ] Relay forwarding to external compute nodes is healthy.
- [ ] Basic customer-facing request path smoke test passes.

## Rollback

- Roll back immediately to last known-good release revision/tag on regression.
- Confirm ingress health and forwarding behavior.
- Record incident + corrective action before next promotion attempt.

## Operator notes

- Keep relay deployment lightweight; do not silently couple compute runtime changes into relay
  rollouts.
- Production cutovers should explicitly reference parity and migration readiness from the canonical
  roadmap.
- Do not represent production as API v1 distributed-compute complete until that phase exits.

## Post-API-v1 target state (future)

Once parity and migration gates are complete, production moves to securely aligned API v1
components across relay and distributed compute nodes. This document will be updated when that state
is implemented.

See also:
- [relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md)
- [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
