# token.place relay on k3s+sugarkube (staging)

> **Environment status:** **Current + planned hardening**.
> Staging is used to validate relay operations before production rollout.

## Scope

Relay-only staging at `staging.token.place` (or equivalent environment hostname), with external
compute nodes still using legacy sink/source contract.

## Prerequisites

- staging cluster namespace access
- relay image tag selected (prefer immutable)
- Cloudflare tunnel + DNS route for staging hostname
- environment config/secrets prepared

## Topology

- Client -> Cloudflare -> tunnel -> Traefik ingress -> relay service/pod
- compute nodes (`server.py`, later desktop parity nodes) remain external

## Release model

- Promote tested dev artifacts into staging.
- Prefer immutable tags (`main-<sha>` / `sha-<sha>`) over mutable latest tags.
- Maintain changelog notes for each staging deploy.

## Deployment workflow (template)

Run from the repository root so the chart path resolves (`./deploy/charts/tokenplace-relay`).
Use agreed sugarkube wrapper once available; until then:

```bash
helm upgrade --install tokenplace-relay ./deploy/charts/tokenplace-relay \
  --namespace tokenplace --create-namespace \
  --set ingress.hosts[0].host=staging.token.place \
  --set gpuExternalName.host=<staging-gpu-hostname>
```

> Replace placeholder values with finalized staging values (or a staging values file) before
> rollout.

## Validation checklist

- [ ] relay pod(s) healthy and stable
- [ ] ingress route serves `https://staging.token.place/livez` and `https://staging.token.place/healthz`
- [ ] relay receives expected registration/poll traffic from external nodes
- [ ] smoke test request flow succeeds end-to-end on legacy contract

## Rollback

- revert Helm release revision and/or pinned image tag
- confirm `/livez`, `/healthz`, and registration flow after rollback
- capture incident notes in outages/ if customer-visible

## Operator notes

- Staging should mirror production ingress/security posture where practical.
- Do not assume API v1 distributed compute is enabled yet; this environment is still pre-migration.
