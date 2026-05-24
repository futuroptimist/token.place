# token.place relay on k3s+sugarkube (dev)

> **Environment status:** **Planned / active development target**.
> Focus is relay-only deployment; compute nodes stay external in this phase.

## Scope

Deploy `relay.py` to the sugarkube dev environment for iterative validation of:

- ingress/tunnel plumbing
- relay image/chart updates
- registration and polling behavior with external compute nodes (API v1/E2EE guardrails intact)

## Prerequisites

- sugarkube dev cluster access
- helm/kubectl tooling
- relay image tag available
- dev hostname and Cloudflare tunnel route configured

## Topology

- In-cluster: `relay.py` deployment + service + ingress
- External: `server.py` and/or desktop compute nodes
  - Typical external operators: Windows 11 CUDA or macOS Apple Silicon Metal; CPU fallback valid.

## Release model

- Use mutable dev tags for fast iteration when needed.
- Prefer pinned immutable tags before promotion to staging.
- Use immutable tags when promoting to staging/prod; mutable tags are dev convenience only.

## Deployment workflow (template)

```bash
# Run from a sugarkube checkout (not token.place):
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
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
