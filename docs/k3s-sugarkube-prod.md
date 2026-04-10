# token.place relay on k3s+sugarkube (prod)

> **Environment status:** **Planned / post-staging promotion target**.
> Production runbook is prepared now for future onboarding and consistent operations.

## Scope

Run `relay.py` on sugarkube production with strict change control. Compute nodes remain external
until parity and later API v1 migration phases are complete.

## Prerequisites

- production cluster access with audited credentials
- approved production image tag and release notes
- production Cloudflare hostname/tunnel configured
- production secrets/config material validated
- rollback owner/on-call identified

## Topology

- Public ingress terminates through Cloudflare+tunnel to sugarkube Traefik
- relay pods run in dedicated namespace with health probes and resource limits
- external compute nodes connect via approved relay URL

## Release model

- staged promotion only (dev -> staging -> prod)
- immutable tag requirement for production rollout
- maintenance window or controlled rollout policy per operator team

## Deployment workflow (template)

```bash
# TODO: replace with production-approved sugarkube command wrapper when finalized.
# Run from the repository root so ./deploy/charts/tokenplace-relay resolves.
helm upgrade --install tokenplace-relay ./deploy/charts/tokenplace-relay \
  --namespace tokenplace --create-namespace
```

## Validation checklist

- [ ] production relay endpoints reachable and healthy
- [ ] `GET /livez` confirms process liveness
- [ ] `GET /healthz` confirms readiness (non-draining)
- [ ] registration/polling from external compute nodes succeeds
- [ ] error rate/latency within expected baseline
- [ ] rollback command and previous revision confirmed before sign-off

## Rollback

- immediate rollback to prior known-good Helm revision/image tag
- validate `/livez`, `/healthz`, and request flow
- record deployment outcome and follow-up actions

## Operator notes

- Keep relay lightweight in-cluster; avoid coupling production relay rollout to simultaneous
  compute-runtime migrations.
- Post-API-v1 target state should align all token.place components on API v1 contracts, but that is
  a later phase and not assumed by this runbook today.
