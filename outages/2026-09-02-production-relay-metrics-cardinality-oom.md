# Outage: Production relay metrics-cardinality OOM

## Incident metadata

- **Date:** 2026-09-02
- **Severity:** Critical
- **Status:** Mitigated
- **Component:** production token.place relay and its application metrics endpoint
- **Incident ID:** `2026-09-02-production-relay-metrics-cardinality-oom`
- **Primary incident window:** approximately 2026-09-02 16:41 PDT / 23:41 UTC through
  2026-09-02 18:08:18 PDT / 2026-09-03 01:08:18 UTC

## Summary

On September 2, 2026, the sole production token.place relay entered an approximately 87-minute
crash loop. Default Flask request instrumentation retained raw or effectively unbounded HTTP path
values as Prometheus labels. A rapid increase in distinct unmatched paths grew the application
metric set from approximately 29,000 Flask series to approximately 71,000. Collection and
serialization of that multiprocess metric set increased `/metrics` response size and latency while
relay memory approached its 256Mi hard limit, after which Kubernetes OOM-killed the process.

The metric files were in `PROMETHEUS_MULTIPROC_DIR=/tmp`, and the chart mounted `/tmp` from a
pod-level `emptyDir`. An ordinary application-container restart retained the accumulated metric
database, allowing the OOM cycle to repeat. Because production had one relay replica, a crash could
temporarily remove the only backend and produce Traefik's browser-visible `no available server`
response.

At 2026-09-03 01:07:50 UTC, an operator paused Prometheus discovery of only the token.place target
and deleted only the affected pod. Kubernetes created a healthy replacement with a fresh
pod-level `emptyDir`; the image and Deployment were not rolled back or changed. Production serves
traffic again, but application scraping remains intentionally paused. The incident is therefore
**Mitigated**, not resolved: a bounded-cardinality fix must be deployed and pass the restoration
criteria below before scraping is restored.

## Impact

- Production was intermittently unavailable during an approximately 87-minute crash loop, from
  the first Kubernetes-confirmed restart/OOM evidence at approximately 23:41 UTC until the
  replacement pod became healthy at 01:08:18 UTC. This was not measured as continuous downtime.
- While the sole relay pod was unavailable, browsers could receive Traefik's
  `no available server` response.
- The Deployment and Service reported an available endpoint during some healthy intervals between
  crashes, consistent with intermittent rather than continuous impact.
- Repeated process termination created an unquantified risk of losing active API-v1 correctness
  state because authoritative relay state remained memory-backed. No customer data loss or
  in-memory work loss was proven.
- Affected-user counts, failed-request counts, revenue impact, and exact request-loss duration were
  not measured and are not estimated here.
- Severity is **Critical** because a production PagerDuty alert accompanied user-visible
  unavailability of the only relay replica.

## Detection

The incident was detected through the production PagerDuty alert and user-visible unavailability.
The exact PagerDuty trigger and acknowledgement timestamps are absent from the sanitized evidence,
so this record does not assign a precise page-fire time.

Kubernetes was the authoritative source for the OOM determination: the previous container state
reported reason `OOMKilled`, exit code `137`, and finish time `2026-09-03T00:50:09Z`. The historical
restart counter and `last_termination_oom` signal first appeared at 23:41 UTC, and the Prometheus
target was down by 23:47 UTC. The `container_oom_events` query remained zero and is not treated as
evidence that no OOM occurred.

## Timeline

All incident times are shown in PDT and UTC. Prometheus observations came from the
`2026-09-02T23:20:00Z` through `2026-09-03T01:08:00Z` historical window at 30-second resolution;
sampled maxima may be lower than instantaneous peaks.

| Time (PDT) | Time (UTC) | Event |
| --- | --- | --- |
| 2026-08-27 11:10:41 | 2026-08-27 18:10:41 | The ServiceMonitor was created. It remained generation 1 before mitigation and was already scraping every 30 seconds; it was not activated at incident onset. |
| 2026-09-02 16:20:00 | 2026-09-02 23:20:00 | Historical evidence window began. Working set was 197,107,712 bytes. |
| 2026-09-02 16:30:00 | 2026-09-02 23:30:00 | Target was up. Working set was 197,111,808 bytes and RSS was 187,510,784 bytes. Metrics included 1,679 distinct path labels, 1,659 unknown paths, 28,964 Flask series, and 28,996 total samples; scrape duration was 3.102 seconds. |
| 2026-09-02 16:39:30 | 2026-09-02 23:39:30 | Maximum sampled working set was 247,996,416 bytes: `247,996,416 / 268,435,456 × 100 = 92.385%`, or approximately 92.4% of the 256Mi limit. |
| 2026-09-02 16:40:00 | 2026-09-02 23:40:00 | Target remained up. Maximum sampled RSS was 242,376,704 bytes. Cardinality reached 2,741 distinct paths, 2,719 unknown paths, 47,018 Flask series, and 47,050 total samples. `scrape_series_added` was 2,261 and scrape duration was 6.150 seconds. |
| Approximately 2026-09-02 16:41 | Approximately 2026-09-02 23:41 | The first historical restart and `last_termination_oom` signal appeared. `scrape_series_added` reached its observed maximum of 2,822. This is the first Kubernetes-confirmed OOM/restart evidence. |
| 2026-09-02 16:46:30 | 2026-09-02 23:46:30 | Metrics reached 4,124 distinct paths, 4,093 unknown paths, and 70,529 Flask series. |
| 2026-09-02 16:47:00 | 2026-09-02 23:47:00 | Prometheus target was down and the restart counter was 2. |
| 2026-09-02 16:48:00 | 2026-09-02 23:48:00 | Observed maxima reached 4,155 distinct paths, 4,121 unknown paths, 71,056 Flask series, and 71,088 scraped samples. |
| 2026-09-02 16:55:00 | 2026-09-02 23:55:00 | Restart counter was 5. |
| 2026-09-02 17:05:00 | 2026-09-03 00:05:00 | Restart counter was 7. |
| 2026-09-02 17:50:09 | 2026-09-03 00:50:09 | Kubernetes recorded the prior container termination as `OOMKilled`, exit code 137. |
| During initial triage | During initial triage | The affected pod had reached 15 restarts. Kubernetes events included 251 BackOff events over approximately 73 minutes. A later historical query showed a maximum restart count of 16. |
| 2026-09-02 18:07:50 | 2026-09-03 01:07:50 | After verifying the exact production target, the operator changed only the ServiceMonitor `release` label from `kube-prometheus-stack` to `incident-paused-tokenplace-oom`, then deleted only the OOM-looping pod. No image rollback or Deployment change occurred. |
| 2026-09-02 18:08:18 | 2026-09-03 01:08:18 | Replacement pod started, became ready, and ended the confirmed impact window with a fresh pod-level `emptyDir`. |

## Technical root cause

### Confirmed mechanism

The confirmed root cause was unbounded application metric label cardinality, not a Prometheus
memory leak.

1. Default Flask request instrumentation retained raw or effectively unbounded request path values
   as labels. Almost every observed path value during the buildup was an unmatched route: 1,659 of
   1,679 at 23:30 UTC, 2,719 of 2,741 at 23:40 UTC, and 4,093 of 4,124 at 23:46:30 UTC.
2. Distinct path values rapidly multiplied the number of application series. Flask series grew
   from 28,964 at 23:30 UTC to 70,529 at 23:46:30 UTC and peaked at 71,056 at 23:48 UTC.
3. Every 30-second scrape collected and serialized the large multiprocess set. Successful
   `/metrics` responses recorded in application access logs were approximately
   7,788,590–7,788,593 bytes and took approximately 6.45–7.93 seconds. Historical Prometheus
   scrape duration rose from 3.102 seconds at 23:30 UTC to 6.150 seconds at 23:40 UTC and reached
   an observed maximum of 8.238 seconds.
4. Relay memory approached the container's 268,435,456-byte limit. The largest 30-second working
   set sample was 247,996,416 bytes (approximately 92.4%); the largest RSS sample was 242,376,704
   bytes. The samples need not capture the instantaneous allocation that crossed the hard limit.
5. Kubernetes OOM-killed the process. Its previous-state termination evidence, not the zero-valued
   `container_oom_events` query, establishes the OOM.
6. `PROMETHEUS_MULTIPROC_DIR` pointed to `/tmp`, which was backed by a pod-level `emptyDir`.
   Application-container restarts within the same pod retained the accumulated metric database,
   so restarting the container did not remove the condition and the crash loop continued.
7. Production had one desired and available replica. Each OOM interval could therefore leave no
   serving backend.

### Trigger attribution

**Supported inference:** the rapid arrival of thousands of distinct unmatched paths is consistent
with automated scanning or randomized-path traffic.

**Unresolved evidence:** raw historical paths, complete access logs, and exact source addresses are
not available in the public evidence. No attacker, crawler, customer, or exact traffic source can
be identified. The unusual traffic exposed the latent defect; regardless of traffic origin, the
application should not have created unbounded label values.

### Deployment-change attribution and PR #1726

PR [#1726](https://github.com/futuroptimist/token.place/pull/1726) was unrelated to this outage. It
merged on 2026-09-02 at 03:53:30 UTC as an internal, non-runtime-wired Valkey
scheduler/reservation/enqueue slice. Although the PR contained 65 commits, production was running
`ghcr.io/futuroptimist/tokenplace-relay:sha-e46277d`. Commit
[`e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22)
was created on August 30 and predates the #1726 merge.

That deployed commit came from PR
[#1735](https://github.com/futuroptimist/token.place/pull/1735), whose relevant change was limited
to immutable build-info labels. No production rollout occurred near incident onset; the
ReplicaSet and pod were approximately three days old. Neither #1726's commits nor a same-time
deployment are attributed as the trigger.

## Contributing factors

- Implicit default per-path Flask instrumentation accepted effectively unbounded caller-controlled
  label values instead of a small, explicitly registered metric set.
- Unmatched/404 paths were not collapsed to a fixed label.
- Cardinality, scrape sample count, series additions, scrape duration, memory headroom, and restart
  acceleration were not correlated early enough to prevent the OOM.
- The 256Mi hard limit left little headroom as metric state and scrape serialization grew. Raising
  it alone would delay, not fix, an unbounded-cardinality failure.
- Multiprocess metric files shared general pod-level `/tmp` storage and were not safely cleared on
  every application-container startup.
- The `emptyDir` lifetime was the pod lifetime, not the application-container lifetime.
- A single replica amplified every process crash into a possible total loss of serving capacity.
  Adding replicas is not yet a safe standalone mitigation because authoritative relay correctness
  state remains memory-backed.
- The ServiceMonitor's established 30-second scrape schedule repeatedly exercised the expensive
  exporter. It had existed since August 27 and was not a newly activated incident-time change.

## Recovery and resolution

At 2026-09-03T01:07:50Z, the operator first verified the Kubernetes context (`sugar-prod`),
namespace and Deployment (`tokenplace`), exact image, replica count, memory limit, and
ServiceMonitor configuration. The operator then:

1. paused Prometheus discovery of only the token.place target by changing the ServiceMonitor's
   `release` label from `kube-prometheus-stack` to `incident-paused-tokenplace-oom`;
2. deleted only affected pod `tokenplace-7758c45ffb-dqccb` (UID
   `e151eed5-6ecf-466e-9cc2-79956ea71903`) so Kubernetes created a new pod and fresh pod-level
   `emptyDir`; and
3. did not roll back the image or change the Deployment.

The replacement was `tokenplace-7758c45ffb-fr45t` (UID
`2d1a6ad9-aff9-40d9-a3f3-60ed062c387b`) on node `sugarkube0`. It started at
`2026-09-03T01:08:18Z`, reported Ready `True`, and had zero restarts during mitigation
verification.

This emergency action removed the accumulated pod-level metric files and stopped scheduled
scrapes from rebuilding or serializing the unsafe metric set. It restored application service but
is not the permanent fix. The telemetry state remains intentionally degraded, so the incident is
Mitigated rather than Resolved.

## Post-recovery verification

- The replacement pod was Ready `True` with zero restarts during verification.
- Public `/livez`, `/healthz`, and `/` requests returned HTTP 200.
- Application state was healthy.
- The deployed image and Deployment were unchanged by mitigation.
- token.place application scraping remained intentionally paused.
- The verification proves recovery of serving traffic, not that the cardinality defect has been
  permanently corrected or that scraping is safe to restore.

## What went well

- Triage verified the exact context, namespace, Deployment, image, replica count, memory limit, and
  scrape target before making a narrowly scoped mitigation.
- Kubernetes termination state provided authoritative OOM evidence even though a related
  Prometheus OOM query remained zero.
- Historical 30-second metrics and sanitized aggregate application-log measurements allowed
  cardinality, scrape cost, memory pressure, and restarts to be aligned without publishing raw
  paths or sensitive payloads.
- Pausing only the affected target and replacing only the affected pod restored service without an
  image rollback or Deployment change.
- Public health checks confirmed that the replacement served traffic.

## What went poorly

- Caller-controlled unmatched paths could produce new metric series without a fixed bound.
- Metrics collection competed for memory with the relay and could serialize an approximately
  7.79 MB response every 30 seconds under the incident state.
- Container restart did not clear multiprocess metric files, making automatic recovery ineffective.
- A one-replica deployment turned process instability into intermittent production unavailability.
- Detection did not warn on the combined cardinality, scrape-duration, memory-headroom, and restart
  trend before the hard OOM limit was crossed.
- Application scraping had to be paused to recover safely, leaving production telemetry degraded.
- Evidence retention could not identify the traffic source or quantify customer request failures
  and possible active in-memory work loss.

## Corrective actions

These actions are proposed and unassigned; this document does not claim that a permanent fix is
complete.

| Priority | Type | Action | Rationale | Owner | Status | Verification or exit criterion |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | Prevent | Replace raw-path Flask grouping with bounded route-template or endpoint labels. | Removes caller control of label cardinality. | Unassigned | Proposed | A fixed allowlist of route labels is demonstrated under adversarial traffic. |
| P0 | Prevent | Collapse every unmatched/404 route to one fixed label; prohibit query strings, request IDs, model names, keys, tokens, and arbitrary path segments from labels. | One unknown route class must remain one series class and must not expose sensitive values. | Unassigned | Proposed | Thousands of distinct unknown URLs yield the same bounded labels and no sensitive strings in exposition. |
| P0 | Prevent | Prefer a small explicitly registered metric set over implicit default per-path instrumentation. | Makes the exported contract reviewable and bounded. | Unassigned | Proposed | Exported metric names and every label domain have documented finite bounds. |
| P0 | Prevent | Add a regression/load test with thousands of unique unmatched paths and fixed budgets for series, samples, response size, scrape duration, and memory. | Reproduces the trigger class and prevents recurrence. | Unassigned | Proposed | The test passes explicit reviewed budgets and fails the prior unbounded behavior. |
| P0 | Prevent | Move multiprocess metrics to a dedicated directory and clear it safely on every application-container startup before Gunicorn launches. | A container restart must not inherit stale metric files; pod deletion must not be the cleanup mechanism. | Unassigned | Proposed | A container restart test proves the directory starts clean without deleting the pod. |
| P0 | Prevent | Validate Prometheus multiprocess worker cleanup. | Dead-worker files and series must not accumulate across worker lifecycles. | Unassigned | Proposed | Repeated worker start/exit testing leaves a bounded, correct exposition. |
| P1 | Prevent | Consider a bounded edge rate limit or scanner control for unmatched paths. | Reduces abusive load as defense in depth but cannot replace bounded labels. | Unassigned | Proposed | Legitimate routes remain usable and randomized-path traffic is bounded; exporter tests still pass without this control. |
| P1 | Prevent | Reassess the 256Mi memory limit only after measuring the corrected exporter. | Measured headroom is useful, but a temporary increase is not a root-cause fix. | Unassigned | Proposed | A sustained corrected-exporter test supports a documented limit and safety margin. |
| P0 | Detect | Alert on relay working-set-to-limit ratios at warning and critical thresholds. | Provides actionable headroom before kernel enforcement. | Unassigned | Proposed | Controlled threshold crossing fires and resolves both alert levels. |
| P0 | Detect | Alert directly on `OOMKilled` state and restart acceleration. | Avoids waiting for only the scrape target to fail. | Unassigned | Proposed | Synthetic OOM-state and restart-rate inputs exercise the alert path. |
| P0 | Detect | Define per-target budgets and alerts for `scrape_samples_scraped`, `scrape_series_added`, `scrape_duration_seconds`, and distinct bounded route-label counts. | Detects exporter growth before memory exhaustion. | Unassigned | Proposed | Each budget has a documented threshold and tested alert. |
| P1 | Detect | Add a dashboard combining application cardinality, scrape sample count/size, scrape latency, memory headroom, and restarts. | Correlated signals shorten diagnosis. | Unassigned | Proposed | Dashboard panels populate from a staging cardinality exercise. |
| P0 | Detect | Add a release/staging gate that rejects linear series growth from unique unknown paths. | Stops reintroduction before production. | Unassigned | Proposed | Gate fails an intentionally unbounded fixture and passes bounded instrumentation. |
| P1 | Detect | Retain privacy-safe aggregate access evidence long enough to classify future triggers without raw credentials, query strings, encrypted payloads, or sensitive paths. | Improves attribution confidence without weakening privacy. | Unassigned | Proposed | Retention and redaction review proves only bounded aggregates are stored. |
| P0 | Mitigate | Write a metrics-induced OOM-loop runbook: validate context/target, pause only its ServiceMonitor, replace the pod, verify public health, and keep scraping paused until exit criteria pass. | Makes the safe, narrow recovery repeatable. | Unassigned | Proposed | A non-production exercise completes with no unrelated target mutation. |
| P0 | Mitigate | Add a bounded emergency setting that disables expensive application metrics while retaining liveness, readiness, and minimal operational metrics. | Preserves minimum observability without exercising unsafe exposition. | Unassigned | Proposed | Exercise shows minimal metrics and health remain available with bounded resource use. |
| P0 | Mitigate | Document that container restart does not necessarily clear a pod `emptyDir`; this incident required pod replacement. | Prevents ineffective restart loops. | Unassigned | Proposed | Runbook review and a pod-lifecycle test demonstrate the distinction. |
| P0 | Mitigate | Define a safe procedure to restore the ServiceMonitor label only after the corrected image is verified. | Prevents premature scrape restoration. | Unassigned | Proposed | Procedure includes all restoration gates and a rollback step. |
| P1 | Mitigate | Continue shared-state/HA work tracked by [#1569](https://github.com/futuroptimist/token.place/issues/1569), without treating replicas as safe standalone mitigation while authoritative state is memory-backed. | HA can reduce single-replica amplification only after correctness constraints are satisfied. | Unassigned | Proposed | Shared-state correctness is proven before multi-replica availability is relied upon. |
| P0 | Mitigate | After the fix, run a staging soak with scraping enabled and adversarial unique-path traffic before production restoration. | Validates the whole scrape/traffic lifecycle. | Unassigned | Proposed | Sustained soak passes every restoration exit criterion. |

### Required restoration exit criteria

Production application scraping must not be restored until all of the following are demonstrated:

- unmatched paths map to a bounded label set;
- thousands of unique paths do not grow Prometheus series linearly;
- `/metrics` sample count, response size, and latency remain within explicit reviewed budgets;
- relay working set remains comfortably below the configured memory limit during a sustained
  scrape-and-traffic test;
- an application-container restart cannot inherit stale multiprocess metric files;
- the corrected image is deployed and its identity is verified;
- public health and application functionality remain healthy; and
- the ServiceMonitor is restored deliberately and observed through a defined stability window.

## Post-incident closeout

The availability impact ended when the replacement pod became healthy at
2026-09-03T01:08:18Z. Closeout state is:

- **Application:** healthy after mitigation.
- **Telemetry:** token.place application scraping intentionally paused.
- **Incident:** Mitigated, not Resolved.
- **Runtime/deployment remediation:** no image rollback or Deployment change was part of emergency
  recovery.
- **Permanent closeout condition:** deploy and verify bounded instrumentation and safe
  multiprocess cleanup, then deliberately restore scraping through the full stability window.

Disabling scraping, replacing a pod, filtering traffic, raising memory, or adding replicas alone
must not be recorded as permanent resolution. Issue #1569 concerns separate shared-state/HA
resilience work and did not cause this incident.

## Evidence gaps and unknowns

- The historical evidence window begins at 23:20 UTC, so the exact first unmatched request that
  began the buildup is unknown.
- Raw historical request paths and exact source addresses are unavailable or intentionally
  excluded from the public record. The traffic source cannot be attributed.
- Exact customer request failures and any active in-memory work losses were not measured. The
  record identifies risk, not proven customer data loss.
- Exact PagerDuty trigger and acknowledgement timestamps are unavailable in the sanitized evidence.
- Direct pre-mitigation `kubectl top` and cgroup capture failed because the old container was
  already unavailable.
- `container_oom_events` remained zero. This is not evidence against the authoritative Kubernetes
  `lastState.terminated.reason=OOMKilled` record.
- `scrape_body_bytes` remained zero. It is not evidence of an empty response; response-size claims
  use sanitized application access-log measurements instead.
- Thirty-second memory samples may have missed the instantaneous allocation that crossed 256Mi.
- Raw paths, query strings, request IDs, credentials, tokens, ciphertext, prompts, responses, tool
  data, arbitrary diagnostic payloads, source addresses, private evidence paths, screenshots, and
  raw log archives are intentionally excluded.

## Verification commands and public references

Repository commands used to validate this documentation change:

```bash
python -m json.tool outages/2026-09-02-production-relay-metrics-cardinality-oom.json >/dev/null
python -c 'import json; import jsonschema; jsonschema.validate(json.load(open("outages/2026-09-02-production-relay-metrics-cardinality-oom.json")), json.load(open("outages/schema.json")))'
pre-commit run --all-files
git diff --check
detect-secrets scan $(git diff --cached --name-only)
```

Public references:

- [Deployed commit `e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22)
- [PR #1735: build-info label correction](https://github.com/futuroptimist/token.place/pull/1735)
- [PR #1726: unrelated internal Valkey slice](https://github.com/futuroptimist/token.place/pull/1726)
- [Issue #1569: separate shared-state/HA resilience work](https://github.com/futuroptimist/token.place/issues/1569)
- [Prometheus multiprocess-mode documentation](https://prometheus.github.io/client_python/multiprocess/)
- [Kubernetes `emptyDir` documentation](https://kubernetes.io/docs/concepts/storage/volumes/#emptydir)

This public record uses sanitized aggregates and durable repository references. It does not publish
private evidence locations or raw diagnostic artifacts.
