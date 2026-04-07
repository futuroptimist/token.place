# token.place relay on k3s+sugarkube (dev)

> **Environment status:** Current/active for iterative relay validation.
> **Scope:** relay.py on cluster, compute nodes remain external.

## Purpose

Use dev to validate relay chart changes, ingress behavior, and external compute connectivity before
staging promotion.

## Topology (current dev)

- In-cluster: `tokenplace-relay` deployment + service + ingress.
- Out-of-cluster: `server.py` compute node(s) using legacy sink/source registration.
- Optional: desktop MVP clients for manual end-to-end checks.

## Prerequisites

- sugarkube dev cluster access (`kubectl`, `helm`).
- Relay image pull access from GHCR.
- Cloudflare/tunnel hostname for dev relay endpoint.
- External compute node reachable from cluster egress.

## Release model

- Preferred tag class: mutable dev tag for smoke checks, then immutable SHA tag for shared testing.
- If sugarkube wrapper commands are not finalized for token.place, use Helm directly and record the
  exact command in deployment notes.

## Deploy (template)

Use this structure and replace placeholders with environment-specific values:

```bash
helm upgrade --install tokenplace-relay ./deploy/charts/tokenplace-relay \
  --namespace tokenplace-dev --create-namespace \
  --set image.repository=ghcr.io/democratizedspace/tokenplace-relay \
  --set image.tag=<dev-or-sha-tag> \
  --set ingress.enabled=true \
  --set ingress.className=traefik \
  --set ingress.hosts[0].host=<dev-hostname> \
  --set upstream.url=<external-compute-url>
```

## Validation checklist

- [ ] Pods are healthy and rollout completed.
- [ ] `/healthz` returns success through ingress hostname.
- [ ] Relay can register/forward to external compute node.
- [ ] Failover behavior validated if multiple compute nodes are configured.

## Rollback

- Roll back to last known-good immutable tag.
- Confirm pod health and `/healthz` after rollback.
- Re-run forwarding smoke test to external compute node.

## Operator notes

- Relay is intentionally lightweight and cluster-friendly.
- Keep compute nodes external until parity and API migration phases are complete.
- Record unresolved command standardization (Helm/just wrappers) as follow-up tasks, not implicit
  tribal knowledge.

## Forward state (not current)

After desktop parity and API v1 migration readiness are complete, dev can validate API v1-aligned
relay/compute interactions. That state is future work.

See also:
- [relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md)
- [roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md)
