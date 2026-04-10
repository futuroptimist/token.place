# token.place relay on k3s+sugarkube (dev)

> **Environment status:** **Planned / active development target**.
> Focus is relay-only deployment; compute nodes stay external in this phase.

## Scope

Deploy `relay.py` to the sugarkube dev environment for iterative validation of:

- ingress/tunnel plumbing
- relay image/chart updates
- registration and polling behavior with external compute nodes

## Prerequisites

- sugarkube dev cluster access
- helm/kubectl tooling
- relay image tag available
- dev hostname and Cloudflare tunnel route configured

## Topology

- In-cluster: `relay.py` deployment + service + ingress
- External: `server.py` and/or desktop compute nodes using legacy relay contract
  - Typical external operators: Windows 11 CUDA or macOS Apple Silicon Metal; CPU fallback valid.

## Release model

- Use mutable dev tags for fast iteration when needed.
- Prefer pinned immutable tags before promotion to staging.
- If token.place-specific `just` helpers are unavailable, run Helm commands directly and track
  the missing automation as follow-up work.

## Deployment workflow (template)

```bash
# TODO: replace with token.place-specific sugarkube wrapper once finalized.
# Run from the repository root so ./deploy/charts/tokenplace-relay resolves.
helm upgrade --install tokenplace-relay ./deploy/charts/tokenplace-relay \
  --namespace tokenplace --create-namespace
```

## Validation checklist

- [ ] `kubectl get pods` shows ready relay pod(s)
- [ ] ingress host resolves in dev
- [ ] `GET /livez` returns `{"status":"alive"}`
- [ ] `GET /healthz` returns ready status (200)
- [ ] external compute node can register/poll relay

## Rollback

Record the current revision before rollout (`helm history tokenplace-relay -n tokenplace`) so rollback targets are explicit.

- Roll back to previous image tag and/or Helm revision.
- Verify readiness and `/healthz` immediately after rollback.

## Operator notes

- Keep this environment permissive for debugging, but avoid introducing config assumptions that
  cannot be promoted.
- Legacy contract is expected here until post-parity API v1 migration.
