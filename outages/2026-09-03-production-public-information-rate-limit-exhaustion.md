# Outage: Production public-information rate-limit exhaustion

## Incident metadata

- **Date:** 2026-09-03
- **Severity:** Major
- **Status:** Mitigated
- **Component:** production token.place relay public landing page and metadata endpoint
- **Incident ID:** `2026-09-03-production-public-information-rate-limit-exhaustion`
- **Observed incident window:** earliest retained HTTP 429 at `2026-09-03T17:11:52.030Z` through
  recovery verification at `2026-09-03T22:51:22Z`; exact onset is unknown

## Summary

On September 3, 2026, scheduled blackbox probes requested `/` and `/api/v1/meta` every 60 seconds.
The deployed `release/relay-0.1.1` implementation counted these safe public-information reads
against its default `1000/day` endpoint quotas. Flask-Limiter keyed those requests with
`get_remote_address()` behind the production proxy. Retained evidence strongly supports that the
probes and browser traffic reached an exhausted application-visible identity, making
monitoring-generated quota exhaustion user-visible; the complete client-identity topology and
trusted-proxy configuration have not been proven.

The release lineage omitted the exact read-only public-information exemption already merged to
`main` in [PR #1551](https://github.com/futuroptimist/token.place/pull/1551), and production
promotion validation did not catch the parity gap. There was no pre-exhaustion quota-budget alert
or validation proving that the configured synthetic frequency could not exhaust a monitored
endpoint.

The operator paused discovery of only the root and metadata blackbox probes and replaced the exact
running process to reset its process-local, in-memory limiter counters. The image, release, and
quotas were unchanged. Serving and compute paths recovered, but the two probes remain paused and
no permanent correction has been deployed. The incident is therefore **Mitigated**, not Resolved.

## Impact

- Public landing-page and metadata reads returned HTTP 429 to traffic sharing the exhausted
  application-visible identity.
- The relay was not totally unavailable. Retained evidence shows health and version checks and
  active compute polling continued to succeed.
- The earliest retained 429 is an observation, not necessarily the exact beginning of impact.
  Exact customer count, exact onset, and complete incident totals are unknown.
- No confidentiality breach or plaintext exposure was observed. No customer payloads or
  credentials are required to establish the cause.
- Replacing the single process carried the established risk of losing process-local relay and
  limiter state. Compute registration, polling, and response submission nevertheless recovered
  successfully.

## Detection

The incident was identified after public-information requests began returning 429. Read-only
triage at `2026-09-03T22:45:51Z` confirmed that the deployed image was Ready with zero restarts:
`/` and `/api/v1/meta` returned 429, while `/api/v1/version`, `/livez`, and `/healthz` returned 200.
This route split showed quota exhaustion rather than total relay failure.

Retained triage-log aggregates contained 312 HTTP 429 responses for `GET /`, 288 for
`GET /api/v1/meta`, and 2,024 successful compute polling requests. These bounded aggregates are
retained-log counts, not guaranteed-complete incident totals. The limiter error identified
`1000 per 1 day`; its `86400 seconds` wording described the configured fallback/window duration,
not a proven remaining outage duration.

Detection came after exhaustion. No alert warned that synthetic traffic was consuming a material
part of either quota, and deployment validation had not compared probe frequency with endpoint
budgets.

## Timeline

All times are UTC. Observed events are distinguished from inferred onset.

| Time (UTC) | Event |
| --- | --- |
| `2026-09-03T01:08:18Z` | The replacement process from the September 2 OOM mitigation started with fresh process-local, in-memory limiter counters. |
| Inferred during September 3 | The 60-second root and metadata probes continued consuming daily quota. The precise exhaustion time and exact first affected customer request were not retained. |
| `2026-09-03T17:11:52.030Z` | Earliest retained HTTP 429 for `GET /`; this is not necessarily the exact start of impact. |
| `2026-09-03T17:16:08.744Z` | Earliest retained HTTP 429 for `GET /api/v1/meta`; this is not necessarily the exact start of impact. |
| `2026-09-03T22:45:51Z` | Read-only triage found the image Ready with zero restarts. `/` and `/api/v1/meta` returned 429; `/api/v1/version`, `/livez`, and `/healthz` returned 200. |
| `2026-09-03T22:48:55Z` | Mitigation paused discovery of only the root and metadata blackbox probes, retained `/livez` and `/healthz` probes, and replaced the exact running process. The image, quotas, and release were unchanged. |
| `2026-09-03T22:51:22Z` | Recovery verification showed HTTP 200 from `/`, `/api/v1/meta`, `/api/v1/version`, `/api/v1/models`, `/livez`, and `/healthz`; one compute registration, eleven successful polls, one successful response submission, zero restarts, and no OOM termination. |

## Technical root cause

### Causal hierarchy

1. **Trigger:** scheduled blackbox probes requested `/` and `/api/v1/meta` every 60 seconds.
2. **Direct technical root cause:** deployed release commit
   [`e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22)
   counted those safe public-information reads against the default hourly and daily endpoint
   quotas.
3. **Amplifier:** Flask-Limiter used `get_remote_address()` as the default request key behind the
   production proxy. Retained evidence strongly supports probes and browser traffic reaching an
   exhausted application-visible identity. The evidence does not fully establish the client-
   identity topology or trusted-proxy behavior, so this record does not claim that every caller
   shared one identity.
4. **Systemic cause:** production's divergent `release/relay-0.1.1` lineage omitted the exact
   exemption already merged to `main` in PR #1551, and promotion validation did not detect the
   release-parity gap.
5. **Detection gap:** no pre-exhaustion quota-budget alert or validation proved that the configured
   synthetic frequency could not exhaust a monitored endpoint.

### Configuration and quota arithmetic

No same-day quota configuration change was identified. Production used the image defaults:

- `API_RATE_LIMIT=60/hour`;
- `API_DAILY_QUOTA=1000/day`; and
- no configured external Flask-Limiter storage, so counters were process-local and in memory.

A probe running every 60 seconds can issue `24 × 60 = 1,440` requests per endpoint per day. That
necessarily exceeds a `1,000/day` quota. Starting with fresh counters, 1,000 scheduled requests
take approximately 16 hours and 40 minutes; ordinary requests sharing the applicable limiter
identity can cause exhaustion earlier. This arithmetic establishes inevitability under stable
uptime, but it does not establish an exact onset or complete identity topology.

### Release provenance and omitted behavior

The deployed release commit was
[`e46277daaeb76beeb9f2a2e9e265181287239b22`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22),
packaged as immutable image
`ghcr.io/futuroptimist/tokenplace-relay:sha-e46277d`. Its rate limiter applied the default
`60/hour` and `1000/day` limits, keyed ordinary requests with `get_remote_address()`, and exempted
operational and selected relay-read paths. It did not contain the dedicated public-information
exemption.

[PR #1551](https://github.com/futuroptimist/token.place/pull/1551), source commit
[`e6ec5ae`](https://github.com/futuroptimist/token.place/commit/e6ec5ae5cb5e29095aa90bd4a993abeb74302e4e),
was merged to `main` as
[`50a9fae`](https://github.com/futuroptimist/token.place/commit/50a9fae02aedb3b5d0bc19d35619e42f49964006).
It added an exact normalized-path exemption for only `GET` and `HEAD` requests to:

- `/`;
- `/api/v1/meta`; and
- `/api/v1/version`.

PR #1551 did not broadly disable rate limiting. Mutation methods and unrelated API routes remain
rate-limited. The behavior was absent from the deployed release lineage.

## Relationship to the September 2 OOM incident

This was not another OOM and was not caused by `/metrics` scraping. Application-metrics scraping
remained paused throughout this incident. The prior
[metrics-cardinality OOM incident](2026-09-02-production-relay-metrics-cardinality-oom.md) had
repeatedly reset process-local limiter counters. Once its mitigation produced stable process
uptime, the process survived long enough for the pre-existing daily-quota defect to become
deterministic and visible. Stable uptime exposed a latent defect; the OOM mitigation did not cause
it.

The incidents have different triggers, immediate mechanisms, impacts, and mitigation tracks. They
share a systemic release-line parity and production-qualification weakness: metrics hardening from
PR [#1447](https://github.com/futuroptimist/token.place/pull/1447) was absent from the deployed
release, and public-information rate-limit hardening from PR #1551 was also absent. Their causal
analyses remain separate.

## Contributing factors

- A normal one-minute synthetic cadence exceeded the default daily budget by 440 requests per
  endpoint per day.
- The application's proxy-visible address key could coalesce monitoring and browser traffic. The
  retained evidence supports this as the user-impact amplifier, but complete proxy trust and
  identity routing still require validation.
- Process-local counters made uptime and process replacement part of quota behavior. Shared storage
  would change counter lifetime and consistency, but would not correct wrongly charged safe reads.
- Release validation did not require parity with already-reviewed safety fixes or a method-and-route
  quota matrix.
- Monitoring lacked quota-pressure telemetry and a pre-exhaustion alert.

## Recovery and current status

At `2026-09-03T22:48:55Z`, the operator paused discovery of only the root and metadata blackbox
probes, retained `/livez` and `/healthz` probes, and replaced the exact running process to clear its
in-memory counters. The production image, release, and quota values did not change. This was a
scoped emergency mitigation, not a permanent correction.

At `2026-09-03T22:51:22Z`, all checked public, health, metadata, model-listing, and version routes
returned 200. One compute registration, eleven polls, and one response submission succeeded. The
replacement remained at zero restarts with no OOM termination.

Current status is **Mitigated**:

- the application and compute path are healthy;
- root and metadata blackbox probes remain intentionally paused;
- `/livez` and `/healthz` monitoring remains active;
- application-metrics scraping remains independently paused under the OOM incident; and
- no permanent rate-limit correction has been deployed.

Resolution requires a qualified immutable image containing the exact PR #1551 behavior, successful
production rollout, restoration of only the two paused public-information probes, and a stability
period proving that those probes no longer consume their quotas.

## What went well

- Read-only triage distinguished endpoint-specific 429 responses from total relay unavailability.
- Health, version, and compute-polling evidence demonstrated that core service paths remained
  operational.
- Mitigation paused only quota-consuming probes and retained liveness and health coverage.
- End-to-end recovery verification included public reads, model listing, compute registration,
  polling, response submission, restart count, and OOM state.
- Privacy-safe aggregate evidence established the mechanism without customer payloads, credentials,
  source addresses, or raw logs.

## What went poorly

- Safe, scheduled public-information reads consumed ordinary endpoint quotas.
- A probe cadence allowed by monitoring configuration could deterministically exhaust the daily
  quota during a stable process lifetime.
- A safety fix already merged to `main` was missing from the deployed release lineage.
- Promotion checks did not compare release behavior against reviewed safety fixes or synthetic
  frequency against endpoint budgets.
- Users could encounter monitoring-generated exhaustion because of the application-visible request
  identity.
- Recovery required replacing a process that held relay and limiter state in memory.

## Corrective actions

Corrective actions are tracked in the linked issues below. Creating a tracker does not change an
action's implementation, deployment, or restoration status. Runtime correction and production
changes are outside this documentation-only record.

### Prevent

| Priority | Action | Status | Tracker | Verification or exit criterion |
| --- | --- | --- | --- | --- |
| P0 | Manually port PR #1551's behavior to the exact `release/relay-0.1.1` line, or deploy an independently fully qualified newer release with equivalent behavior. | Proposed | [token.place #1766](https://github.com/futuroptimist/token.place/issues/1766) | The immutable deployed image exempts exact normalized-path `GET`/`HEAD` requests only for `/`, `/api/v1/meta`, and `/api/v1/version`. |
| P0 | Preserve rate limiting for non-read methods, `/api/v1/models`, ordinary public API routes, authenticated compute control-plane routes, and all mutation routes. | Proposed | [token.place #1766](https://github.com/futuroptimist/token.place/issues/1766) | A route-and-method matrix proves only the three reviewed read paths are exempt. |
| P0 | Add regression tests under deliberately low hourly and daily quotas. | Proposed | [token.place #1766](https://github.com/futuroptimist/token.place/issues/1766) | Repeated safe reads do not consume quota, while unrelated routes and mutation methods reach the existing OpenAI-style 429 response. |
| P0 | Add a release-line provenance/parity gate for already-merged safety fixes including #1447 and #1551. | Proposed | [token.place #1770](https://github.com/futuroptimist/token.place/issues/1770) | Promotion records source and image identities and fails when required ancestry or an explicitly reviewed, behavior-equivalent backport is absent. |
| P0 | Compare configured synthetic request frequency with every applicable endpoint quota in CI or deployment validation. | Proposed | [sugarkube #2778](https://github.com/futuroptimist/sugarkube/issues/2778) | Validation fails when projected requests can exhaust a quota within its window. |
| P1 | Evaluate shared limiter storage in the existing HA work, without treating it alone as a fix for wrongly charged probes. | Proposed | [Existing non-incident HA work #1569](https://github.com/futuroptimist/token.place/issues/1569) | HA testing proves intended counter consistency and separately verifies the exact public-read exemption. |
| P0 | Validate trusted-proxy client-identity handling without blindly trusting spoofable forwarding headers. | Proposed | [token.place #1772](https://github.com/futuroptimist/token.place/issues/1772) | Production-equivalent tests document trusted hops and prove untrusted clients cannot choose limiter identities. |

### Detect

| Priority | Action | Status | Tracker | Verification or exit criterion |
| --- | --- | --- | --- | --- |
| P0 | Add bounded, route-class-based 429 and quota-pressure telemetry without raw paths, source addresses, credentials, or attacker-controlled labels. | Proposed | [token.place #1771](https://github.com/futuroptimist/token.place/issues/1771)<br>[sugarkube #2782](https://github.com/futuroptimist/sugarkube/issues/2782) | Staging exhaustion produces bounded diagnostics and no sensitive or unbounded labels. |
| P0 | Alert before a synthetic monitor consumes a material percentage of a quota. | Proposed | [Bounded telemetry: token.place #1771](https://github.com/futuroptimist/token.place/issues/1771)<br>[Alert implementation: sugarkube #2405](https://github.com/futuroptimist/sugarkube/issues/2405) | A controlled probe soak crosses warning thresholds before any HTTP 429. |
| P0 | Keep `/livez` and `/healthz` coverage active whenever higher-level probes are intentionally paused. | In effect during mitigation; permanent procedure proposed | [sugarkube #2776](https://github.com/futuroptimist/sugarkube/issues/2776) | Monitoring review confirms continuous basic-health coverage during a scoped pause. |
| P0 | Detect promoted artifacts missing reviewed safety fixes. | Proposed | [token.place #1770](https://github.com/futuroptimist/token.place/issues/1770) | A deliberately incomplete release artifact is rejected before production. |

### Mitigate

| Priority | Action | Status | Tracker | Verification or exit criterion |
| --- | --- | --- | --- | --- |
| P0 | Document the emergency procedure for pausing only quota-consuming Probe resources and resetting process-local counters. | Proposed | [sugarkube #2779](https://github.com/futuroptimist/sugarkube/issues/2779) | A non-production exercise changes only the intended probes and verifies their discovery state. |
| P0 | Warn that process replacement can discard memory-backed relay state and requires controlled quiescence, compute re-registration, and end-to-end verification. | Proposed | [sugarkube #2779](https://github.com/futuroptimist/sugarkube/issues/2779) | The runbook includes explicit state-risk acknowledgement and verifies registration, polling, and response submission after replacement. |
| P0 | Require immutable image identity, route/method matrix tests, a production-equivalent probe soak, and explicit rollback thresholds before restoration. | Proposed | [sugarkube #2774](https://github.com/futuroptimist/sugarkube/issues/2774)<br>[sugarkube #2775](https://github.com/futuroptimist/sugarkube/issues/2775) | Qualification records the image identity and passes all gates through a defined stability window. |
| P0 | Restore only the two public-information probes after the corrected image is healthy. | Blocked on corrected deployment | [sugarkube #2776](https://github.com/futuroptimist/sugarkube/issues/2776) | The two probes return 200 throughout the stability window without consuming their quotas; health probes remain active. |
| P0 | Keep `/metrics` restoration governed by the separate OOM corrective-action track. | In effect | [sugarkube #2777](https://github.com/futuroptimist/sugarkube/issues/2777) | No rate-limit remediation step re-enables application-metrics scraping. |

The recovery sequence is tracked explicitly: [token.place #1766](https://github.com/futuroptimist/token.place/issues/1766) feeds the combined staging qualification
in [sugarkube #2774](https://github.com/futuroptimist/sugarkube/issues/2774), which gates exact-image production deployment in
[sugarkube #2775](https://github.com/futuroptimist/sugarkube/issues/2775). Public-information probe restoration and rate-limit closeout remain gated by
[sugarkube #2776](https://github.com/futuroptimist/sugarkube/issues/2776). Only after both probes are restored and their immediate
checks pass may [sugarkube #2777](https://github.com/futuroptimist/sugarkube/issues/2777) begin application-metrics restoration; the
two 24-hour stability windows may then overlap.
None of these trackers records deployment, restoration, or incident resolution by its creation
alone.

## Restoration and resolution criteria

Before the two paused public-information probes are restored, an immutable corrected image must:

1. contain the exact normalized-path, method-limited PR #1551 behavior;
2. pass low-quota route-and-method regression tests, including the existing OpenAI-style 429
   response for protected routes;
3. pass a production-equivalent probe soak demonstrating that the configured cadence does not
   consume the public-information quotas;
4. record source and image identities and prove release-line safety parity;
5. pass trusted-proxy identity validation and explicit rollback thresholds; and
6. deploy through the controlled process-state procedure with health, compute re-registration,
   polling, and response-submission verification.

After rollout, only the root and metadata probes should be restored. A defined stability period
must prove they remain healthy without consuming quota. If the route/method matrix, quota pressure,
identity behavior, or serving health regresses, pause only those probes and roll back according to
the qualified plan. `/metrics` restoration remains wholly separate.

## Evidence limitations

- Exact customer count and exact incident onset are unknown.
- Retained log aggregates are incomplete by definition and are not incident totals.
- Application-visible client-identity coalescing is strongly supported, but complete trusted-proxy
  configuration and identity topology still require validation.
- No evidence of a same-day quota configuration change was identified.
- No customer payloads or credentials are required to establish the cause, and none are included.
- This public record excludes Kubernetes context and namespace names, selector-label values, pod
  names and UIDs, node names, source addresses, private evidence locations, raw logs, credentials,
  tokens, headers, and request payloads.

## Verification commands and public references

Repository commands used to validate this documentation change:

```bash
python -m json.tool outages/2026-09-02-production-relay-metrics-cardinality-oom.json >/dev/null
python -m json.tool outages/2026-09-03-production-public-information-rate-limit-exhaustion.json >/dev/null
python -c 'import json, jsonschema; schema=json.load(open("outages/schema.json")); [jsonschema.validate(json.load(open(path)), schema) for path in ("outages/2026-09-02-production-relay-metrics-cardinality-oom.json", "outages/2026-09-03-production-public-information-rate-limit-exhaustion.json")]'
pre-commit run --all-files
git diff --check
detect-secrets scan $(git diff --cached --name-only)
```

Public references:

- [Deployed release commit `e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22)
- [PR #1551: exact public-information read exemptions](https://github.com/futuroptimist/token.place/pull/1551)
- [PR #1551 source commit `e6ec5ae`](https://github.com/futuroptimist/token.place/commit/e6ec5ae5cb5e29095aa90bd4a993abeb74302e4e)
- [PR #1551 merge commit `50a9fae`](https://github.com/futuroptimist/token.place/commit/50a9fae02aedb3b5d0bc19d35619e42f49964006)
- [PR #1447: bounded relay metrics registry](https://github.com/futuroptimist/token.place/pull/1447)
- [September 2 metrics-cardinality OOM record](2026-09-02-production-relay-metrics-cardinality-oom.md)

This record uses only public repository provenance, routes, UTC timestamps, configuration defaults,
and bounded aggregates. The causal account is blameless and does not attribute intent or publish
sensitive infrastructure or request data.
