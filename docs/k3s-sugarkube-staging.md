# token.place k3s+sugarkube runbook (staging)

> Environment status: **planned / partially active depending on infra readiness**

## Scope

Staging is the pre-production proving ground for relay-first topology. It should mirror
production routing and security posture as closely as practical while still allowing
migration diagnostics.

## Topology

- In-cluster: `relay.py` (lightweight network front door).
- Out-of-cluster: compute node(s) using legacy relay contract until post-parity
  migrations are complete.
- Public path: Cloudflare tunnel/proxy -> Traefik ingress -> relay service.

## Prerequisites

- Staging DNS hostname (for example `staging.token.place`).
- Cloudflare tunnel/proxy route mapped to cluster ingress.
- Relay image pull + deployment access.
- External compute node reachable from relay egress path.

## Release model

- Use immutable image tags/digests for staging verification.
- Promote only builds that pass staging validation to production.

## Deployment steps

1. Deploy relay release to staging namespace using pinned image.
2. Configure ingress host and Cloudflare route.
3. Apply upstream compute endpoint config and secrets.
4. Validate health, routing, and node registration.

## Validation

- `/healthz` and `/livez` succeed via staging hostname.
- Node registration and request routing are stable for soak period.
- No plaintext content appears in relay logs.
- Basic rollback drill succeeds.

## Rollback

- Re-deploy last known-good pinned release.
- Keep DNS/ingress hostname stable to avoid client-side endpoint churn.

## Operator notes

- Staging should be the first place to rehearse parity-gated cutovers.
- Any undocumented manual step discovered here must be fed back into dev/prod runbooks.

## Post-API-v1 target state (not current)

Staging becomes the canary environment for API v1 distributed compute migration after
desktop parity and legacy compatibility-bridge tests are complete.

## Readiness checklist

- [ ] Pinned release deployment is repeatable.
- [ ] Cloudflare + ingress chain validated end-to-end.
- [ ] Relay-to-external-node routing validated under load.
- [ ] Rollback drill documented with timings.
