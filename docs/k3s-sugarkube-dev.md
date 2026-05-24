# token.place relay on k3s+sugarkube (dev)

> **Environment status:** **Planned / active development target**.
> Keep dev secondary to staging/prod and limited to relay-only validation.

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
- External: `server.py` and/or desktop compute nodes (Windows/macOS/Raspberry Pi)
  - No required in-cluster backend/GPU service.

## Release model

- Use mutable dev tags for fast iteration when needed.
- Prefer pinned immutable tags before promotion to staging.
- Preserve API v1 relay-blind E2EE guardrails (relay handles ciphertext + routing metadata only).

## Deployment workflow (template)

```bash
# Run from sugarkube checkout.
just helm-oci-install release=tokenplace-relay namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace-relay.values.dev.yaml default_tag=main-REPLACE_SHORTSHA set=image.repository=ghcr.io/futuroptimist/tokenplace-relay

# Upgrade existing release.
just helm-oci-upgrade release=tokenplace-relay namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace-relay.values.dev.yaml default_tag=main-REPLACE_SHORTSHA set=image.repository=ghcr.io/futuroptimist/tokenplace-relay
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
- Treat dev as relay-only: cluster runs `relay.py`; compute remains external.
