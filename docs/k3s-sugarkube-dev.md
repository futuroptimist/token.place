# token.place k3s+sugarkube runbook (dev)

> Environment status: **current / active for migration work**

## Scope

This runbook covers relay-first deployment in dev. `relay.py` runs on sugarkube, while
compute nodes remain external and continue to use the legacy `/sink` + `/source`
contract.

## Topology

- In-cluster: `relay.py` deployment + service + ingress.
- Out-of-cluster: compute node(s): `server.py` now; desktop-tauri later in parity phases.
- Optional: Cloudflare-proxied dev hostname.

## Prerequisites

- k3s context pointing at dev cluster/namespace.
- Pull access for relay image.
- Helm/manifests for relay deployment available.
- At least one reachable external compute node endpoint.

## Release model

- Prefer fast iteration tags in dev (e.g., `main-latest`) with explicit rollback notes.
- Keep immutable tag option available for debugging reproducibility.

## Deployment steps (relay-first)

1. Deploy/upgrade relay manifests or chart in dev namespace.
2. Apply dev ingress host config.
3. Set relay upstream config to external compute endpoint(s).
4. Register/validate compute node connectivity.

> If exact `just` or Helm wrappers are not finalized in your infra repo, track them as
> follow-up tasks rather than inventing command names.

## Validation

- Relay pod ready.
- `/healthz` reachable from ingress host.
- Relay can route traffic to a registered external compute node.
- Logs show redacted operational events only.

## Rollback

- Roll back to previous known-good relay image/tag.
- If needed, point clients back to local relay endpoint while cluster issue is triaged.

## Operator notes

- Dev is allowed to trade strict immutability for iteration speed.
- Keep parity work and relay ops work decoupled: relay can be stable even while desktop
  compute-node behavior is still evolving.

## Post-API-v1 target state (not current)

After parity and migration phases, dev should validate API v1 distributed contracts for
relay/node communications with the same rollout shape used in staging/prod.

## Readiness checklist

- [ ] Relay deployment automation works in dev.
- [ ] External compute connectivity validated.
- [ ] Ingress + optional Cloudflare routing validated.
- [ ] Rollback to prior tag tested.
