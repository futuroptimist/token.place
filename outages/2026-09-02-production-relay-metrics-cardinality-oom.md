# Outage: Production relay metrics-cardinality OOM

## Incident metadata

- **Date:** 2026-09-02
- **Incident ID:** `2026-09-02-production-relay-metrics-cardinality-oom`
- **Severity:** Critical
- **Status:** Mitigated
- **Component:** production token.place relay and its application metrics endpoint
- **Primary incident window:** approximately 2026-09-02 16:41 PDT / 23:41 UTC through
  2026-09-02 18:08:18 PDT / 2026-09-03 01:08:18 UTC

`Mitigated` is intentional: the application is serving traffic, but token.place application
scraping remains paused. The incident is not resolved until a bounded-cardinality fix is deployed,
verified, and followed by a deliberately observed restoration of scraping.

## Summary

On September 2, 2026, the sole production token.place relay pod entered an approximately 87-minute
OOM/restart loop. During intervals when the pod was unavailable, browsers received Traefik's
`no available server` response. Kubernetes recorded `OOMKilled`, exit code `137`, as the prior
container termination reason.

The confirmed failure mechanism was application metrics with unbounded HTTP path labels. Default
Flask request instrumentation retained raw or effectively unbounded path values. A rapid increase
in distinct, predominantly unmatched paths expanded the Flask metric set from roughly 29,000 to
roughly 71,000 series. Each 30-second scrape made the relay collect and serialize this large
multiprocess metric set. Successful `/metrics` responses grew to approximately 7.79 MB, scrape
latency reached several seconds, and relay memory approached its 256Mi hard limit before the process
was OOM-killed.

The metric database was under `PROMETHEUS_MULTIPROC_DIR=/tmp`, and the Helm Deployment mounted
`/tmp` from a pod-level `emptyDir`. Ordinary application-container restarts within the same pod did
not clear those files. The accumulated cardinality therefore survived the restarts and sustained
the loop. A single replica amplified each process failure into a period with no serving backend.

The trigger source is not proven. The surge in thousands of unmatched paths is consistent with
automated scanning or randomized-path traffic, but raw historical paths and complete access logs
were not retained in the public evidence. The latent defect was the unbounded label design; traffic
filtering would only be defense in depth.

At 2026-09-03 01:07:50 UTC, the operator paused discovery of only the token.place ServiceMonitor,
then deleted only the affected pod. Kubernetes created a replacement pod with a fresh `emptyDir`.
The Deployment and image were unchanged. The replacement became healthy at 01:08:18 UTC, had zero
restarts during post-mitigation verification, and returned HTTP 200 from `/livez`, `/healthz`, and
`/`. Production application scraping remains intentionally paused pending the permanent fix.

## Impact

- Production was intermittently unavailable during an approximately 87-minute crash loop; this
  was not measured as continuous downtime. The Deployment and Service still reported an available
  endpoint during some healthy intervals between crashes.
- While the sole relay pod was unavailable, the browser-visible symptom was Traefik's
  `no available server` response.
- API-v1 correctness state was still memory-backed. Repeated process termination therefore created
  an unquantified risk of losing active in-memory relay state. The available evidence does not prove
  customer data loss or quantify in-memory work loss.
- Affected-user counts, failed-request counts, request-loss counts, and revenue impact were not
  measured and are not estimated here.
- Severity is **Critical** because a production PagerDuty alert accompanied user-visible
  unavailability of the only relay replica.

## Detection

The production PagerDuty alert and the browser-visible availability symptom prompted triage. The
exact PagerDuty trigger and acknowledgement timestamps are not available in the sanitized evidence,
so this record does not assign a precise page-fire time.

Kubernetes was the authoritative source for the OOM classification: the prior container state was
`OOMKilled` with exit code `137`. Historical Prometheus data then correlated the rising application
metric cardinality, scrape cost, memory pressure, target failure, and restart acceleration. The
first historical restart and `last_termination_oom` signal appeared at 23:41 UTC; the target was
down by 23:47 UTC.

Two queried metrics were not useful evidence. `container_oom_events` remained zero, which does not
override Kubernetes's termination state. `scrape_body_bytes` also remained zero, so response sizes
come from sanitized application access-log aggregates instead. Direct pre-mitigation `kubectl top`
and cgroup capture failed because the old container was already unavailable.

## Timeline

All times are shown in PDT and UTC. Prometheus observations come from the historical window
2026-09-02 23:20:00 UTC through 2026-09-03 01:08:00 UTC at 30-second resolution.

| Time (PDT) | Time (UTC) | Event |
| --- | --- | --- |
| 2026-08-27 11:10:41 | 2026-08-27 18:10:41 | The ServiceMonitor was created. It remained generation 1 and was already actively scraping before the incident. |
| 2026-09-02 16:20:00 | 2026-09-02 23:20:00 | Historical evidence window began. Relay working set was 197,107,712 bytes. |
| 2026-09-02 16:30:00 | 2026-09-02 23:30:00 | Working set was 197,111,808 bytes and RSS was 187,510,784 bytes. There were 1,679 distinct path labels, including 1,659 unknown paths; 28,964 Flask series and 28,996 scraped samples; scrape duration was 3.102 seconds; the target was up. |
| 2026-09-02 16:39:30 | 2026-09-02 23:39:30 | Maximum sampled working set was 247,996,416 bytes, or approximately 92.4% of the 268,435,456-byte limit (`247,996,416 / 268,435,456 x 100 = 92.385%`, rounded to 92.4%). |
| 2026-09-02 16:40:00 | 2026-09-02 23:40:00 | Maximum sampled RSS was 242,376,704 bytes. Cardinality reached 2,741 distinct paths, 2,719 unknown paths, 47,018 Flask series, and 47,050 scraped samples. `scrape_series_added` was 2,261, scrape duration was 6.150 seconds, and the target remained up. |
| Approximately 2026-09-02 16:41 | Approximately 2026-09-02 23:41 | First Kubernetes-confirmed historical restart and `last_termination_oom` signal appeared. `scrape_series_added` reached its observed maximum of 2,822. The 30-second memory samples need not contain the instantaneous allocation that crossed the limit. |
| 2026-09-02 16:46:30 | 2026-09-02 23:46:30 | Cardinality reached 4,124 distinct paths, including 4,093 unknown paths, and 70,529 Flask series. |
| By 2026-09-02 16:47 | By 2026-09-02 23:47 | The Prometheus target was down and the restart counter was 2. |
| 2026-09-02 16:48 | 2026-09-02 23:48 | Observed maxima reached 4,155 distinct path labels, 4,121 unknown paths, 71,056 Flask series, and 71,088 scraped samples. |
| 2026-09-02 16:55 | 2026-09-02 23:55 | Restart counter reached 5. |
| 2026-09-02 17:05 | 2026-09-03 00:05 | Restart counter reached 7. |
| 2026-09-02 17:50:09 | 2026-09-03 00:50:09 | Kubernetes recorded the prior container termination as `OOMKilled`, exit code `137`. |
| Before mitigation | Before mitigation | The affected pod had 15 restarts during initial triage and 16 in the later historical query. Kubernetes events included 251 BackOff events over approximately 73 minutes. Logs showed successful `/metrics` responses of approximately 7,788,590-7,788,593 bytes taking approximately 6.45-7.93 seconds; maximum observed scrape duration was 8.238 seconds. |
| 2026-09-02 18:07:50 | 2026-09-03 01:07:50 | After verifying production context and scope, the operator changed only the ServiceMonitor `release` label from `kube-prometheus-stack` to `incident-paused-tokenplace-oom`, then deleted only the exact OOM-looping pod. No image rollback or Deployment change was made. |
| 2026-09-02 18:08:18 | 2026-09-03 01:08:18 | Replacement pod started, became ready, and ended the approximately 87-minute crash loop. Post-mitigation verification found zero restarts and HTTP 200 from `/livez`, `/healthz`, and `/`. |

## Technical root cause

### Confirmed mechanism

The deployed application initialized `prometheus_flask_exporter` with its default request
instrumentation. That instrumentation retained raw or effectively unbounded HTTP path values as
labels. The application's separately registered relay counter used endpoint labels, but it did not
bound the default Flask exporter's per-path series.

During the evidence window, distinct path labels increased from 1,679 at 23:30 UTC to a maximum of
4,155 at 23:48 UTC. Unknown or unmatched paths accounted for 1,659 and 4,121 respectively, so nearly
all observed path-label values were unmatched routes. Flask series rose from 28,964 to 71,056, while
scraped samples rose from 28,996 to 71,088.

Every scrape required collection and serialization of the multiprocess metric set. Scrape duration
rose from 3.102 seconds at 23:30 UTC to 6.150 seconds at 23:40 UTC and reached 8.238 seconds. Access
logs recorded successful responses of approximately 7.79 MB taking 6.45-7.93 seconds. This was not
a claim that Prometheus leaked memory: the relay application's unbounded metric-label state and the
cost of collecting and serializing that state drove memory pressure.

The maximum sampled working set was 247,996,416 bytes at 23:39:30 UTC, approximately 92.4% of the
256Mi / 268,435,456-byte hard limit. Maximum sampled RSS was 242,376,704 bytes at 23:40 UTC. The
process was subsequently OOM-killed. Because samples were 30 seconds apart, they do not necessarily
capture the transient allocation that crossed the hard limit.

`PROMETHEUS_MULTIPROC_DIR` pointed to `/tmp`. The Deployment mounted `/tmp` as a pod-level
`emptyDir`, which survived ordinary container restarts inside the affected pod. The accumulated
metric database remained available after each process restart, so restarting the application
container did not remove the condition and the crash loop continued. The one-replica topology meant
each OOM interval could remove the only backend.

### Trigger attribution

**Supported inference:** thousands of rapidly appearing unmatched path values are consistent with
automated scanning or randomized-path traffic.

**Unresolved evidence:** the public evidence does not contain raw historical paths, complete access
logs, or source attribution. This record therefore does not identify an attacker, crawler,
customer, or exact traffic source. Regardless of source, unusual traffic exposed a latent
application defect: externally controlled path diversity could create unbounded labels.

### Deployment-change exclusion and PR #1726

There was no production rollout near incident onset. The ReplicaSet and affected pod were
approximately three days old, and production was running
`ghcr.io/futuroptimist/tokenplace-relay:sha-e46277d`. The deployed merge commit
[`e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22)
was created on August 30. Its relevant change, from
[#1735](https://github.com/futuroptimist/token.place/pull/1735), was limited to immutable
build-info labels; it did not introduce the raw-path instrumentation mechanism.

[#1726](https://github.com/futuroptimist/token.place/pull/1726) was unrelated to this outage. It
merged on 2026-09-02 at 03:53:30 UTC as an internal, non-runtime-wired Valkey
scheduler/reservation/enqueue slice. The production image's `e46277d` commit predates that merge.
No production rollout occurred near incident onset, and none of the 65 commits in #1726 are
attributed as a cause of this incident.

## Contributing factors

- Default per-path Flask instrumentation accepted an effectively unbounded, externally influenced
  label instead of a route template or fixed endpoint vocabulary.
- Unknown/404 routes were not collapsed to one bounded label.
- There was no regression or load gate proving that thousands of unique paths leave metric series,
  scrape output, scrape duration, and memory within fixed budgets.
- A shared `/tmp` `emptyDir` coupled unrelated scratch storage and Prometheus multiprocess state;
  container startup did not safely clear the metric files.
- Metrics-target availability detected the consequence after cardinality and memory pressure had
  already grown. There were no direct early alerts joining cardinality, scrape cost, memory
  headroom, OOM state, and restart acceleration.
- The 256Mi hard limit left limited headroom for a multi-megabyte exposition. Raising it alone would
  only defer recurrence and is not the root-cause fix.
- One desired relay replica made every process crash capable of removing the sole backend. Adding
  replicas is not yet a safe standalone mitigation while authoritative relay correctness state is
  memory-backed; that separate resilience work is tracked by
  [#1569](https://github.com/futuroptimist/token.place/issues/1569).
- The ServiceMonitor predated the incident, was generation 1, and had already been scraping every
  30 seconds. It was not newly activated at 16:37 and was not a same-time deployment change.

## Recovery and resolution

At 2026-09-03 01:07:50 UTC, the operator first verified the exact `sugar-prod` context, `tokenplace`
namespace and Deployment, image, replica count, 256Mi memory limit, and ServiceMonitor
configuration. The operator then:

1. Paused Prometheus discovery of only the token.place target by changing the ServiceMonitor's
   `release` label from `kube-prometheus-stack` to `incident-paused-tokenplace-oom`.
2. Deleted only affected pod `tokenplace-7758c45ffb-dqccb` (UID
   `e151eed5-6ecf-466e-9cc2-79956ea71903`), allowing Kubernetes to create a new pod and fresh
   pod-level `emptyDir`.
3. Left the Deployment and image unchanged; no rollback occurred.

Replacement pod `tokenplace-7758c45ffb-fr45t` (UID
`2d1a6ad9-aff9-40d9-a3f3-60ed062c387b`) started on node `sugarkube0` at
2026-09-03 01:08:18 UTC and became ready. Production serves application traffic again, but this was
an emergency mitigation, not the permanent fix. Application scraping must remain paused until the
restoration criteria below are met.

## Post-recovery verification

| Surface | Verified result |
| --- | --- |
| Replacement pod | `tokenplace-7758c45ffb-fr45t`, Ready `True` |
| Image and Deployment | Unchanged; no rollback or Deployment mutation |
| Restart count | `0` after mitigation verification |
| `/livez` | HTTP 200 |
| `/healthz` | HTTP 200 |
| `/` | HTTP 200 |
| Application state | Healthy |
| Telemetry state | token.place application scraping intentionally paused |

These checks establish mitigation and restored serving, not resolution of the cardinality defect.

## What went well

- Triage confirmed the production context, exact workload, immutable image, limit, and scrape
  configuration before changing anything.
- Kubernetes termination state provided authoritative OOM evidence even though a queried OOM metric
  remained zero.
- Historical 30-second aggregates allowed correlation of path cardinality, series growth, scrape
  cost, memory pressure, target failure, and restart acceleration without publishing sensitive raw
  paths or source addresses.
- Mitigation was narrowly scoped: only token.place target discovery was paused and only the exact
  looping pod was replaced. The image and Deployment were not rolled back.
- Fresh pod storage removed the accumulated multiprocess state, and public health checks confirmed
  that serving recovered.

## What went poorly

- An externally controlled, high-cardinality dimension was retained in default application
  metrics.
- Scrape serialization competed with the application inside a 256Mi limit and became sufficiently
  expensive to participate in OOM termination.
- Ordinary container restarts preserved the accumulated metric database in the pod `emptyDir`,
  prolonging the loop.
- The single-replica topology converted each crash into possible user-visible unavailability.
- Alerting did not provide earlier, joined signals for memory headroom, cardinality growth, scrape
  cost, OOM state, and restart velocity.
- Sanitized evidence cannot quantify user failures, active in-memory work loss, or the traffic
  source.

## Corrective actions

No action below is represented as already complete. Owners remain unassigned until maintainers
accept and schedule the work.

| Priority | Type | Action | Rationale | Owner | Status | Verification or exit criterion |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | Prevent | Replace raw-path Flask grouping with bounded route-template or endpoint labels. | Removes the latent unbounded-cardinality defect. | Unassigned | Proposed | Enumerate the finite production label vocabulary and prove unknown paths cannot expand it. |
| P0 | Prevent | Collapse every unmatched/404 route to one fixed label; prohibit query strings, request IDs, model names, keys, tokens, and arbitrary path segments from labels. | Prevents external or sensitive values from becoming series dimensions. | Unassigned | Proposed | Automated tests exercise each prohibited value class and observe no new label value or disclosure. |
| P0 | Prevent | Prefer a small, explicitly registered metric set over implicit default per-path instrumentation. | Makes the metric contract reviewable and bounded. | Unassigned | Proposed | Exported families and labels match an explicit allowlist. |
| P0 | Prevent | Add a regression/load test sending thousands of unique unmatched paths. | Reproduces the trigger shape before release. | Unassigned | Proposed | Fixed budgets cover series count, sample count, response size, scrape duration, and working set. |
| P0 | Prevent | Move multiprocess metrics to a dedicated directory and clear it safely on every application-container startup before Gunicorn launches. | Avoids stale files surviving a process restart; pod deletion must not be the cleanup mechanism. | Unassigned | Proposed | Restart-in-place test proves old metric files and series are not inherited. |
| P0 | Prevent | Validate Prometheus multiprocess worker cleanup. | Prevents dead-worker files or series from accumulating. | Unassigned | Proposed | Multiworker start/exit/restart tests prove correct cleanup and bounded exposition. |
| P2 | Prevent | Consider a bounded edge rate limit or scanner control for unmatched paths. | Reduces abusive load as defense in depth, but cannot replace bounded labels. | Unassigned | Proposed | Load tests show legitimate traffic remains available; cardinality remains bounded with filtering disabled. |
| P2 | Prevent | Reassess the 256Mi limit only after measuring the corrected exporter. | Measured headroom is useful, but a temporary increase is not a root-cause fix. | Unassigned | Proposed | Sustained corrected-exporter profile justifies any limit and documents headroom. |
| P0 | Detect | Alert on relay working-set-to-limit ratio at warning and critical thresholds. | Provides warning before the hard limit is crossed. | Unassigned | Proposed | Controlled test fires and clears both thresholds before OOM. |
| P0 | Detect | Alert directly on `OOMKilled` state and restart acceleration. | Detects process failure without waiting for target-down alone. | Unassigned | Proposed | Synthetic OOM/restart signals route and resolve the intended alerts. |
| P1 | Detect | Add per-target budgets and alerts for `scrape_samples_scraped`, `scrape_series_added`, `scrape_duration_seconds`, and distinct bounded route-label count. | Detects cardinality and scrape-cost growth at its source. | Unassigned | Proposed | Threshold tests fire independently and dashboards identify the target. |
| P1 | Detect | Dashboard application cardinality, sample count/size, scrape latency, memory headroom, and restarts together. | Shortens correlation and diagnosis. | Unassigned | Proposed | Staging exercise displays all signals over one shared interval. |
| P0 | Detect | Fail release/staging if unique unknown paths create unbounded new series. | Stops recurrence before production. | Unassigned | Proposed | Adversarial unique-path gate passes only with constant-bounded series growth. |
| P1 | Detect | Retain privacy-safe aggregate access evidence long enough to classify future triggers. | Improves attribution without storing sensitive content. | Unassigned | Proposed | Retention review confirms no raw credentials, query strings, encrypted payloads, or sensitive paths. |
| P0 | Mitigate | Write a metrics-induced OOM-loop runbook: validate context/target, pause only its ServiceMonitor, replace the pod, verify public health, and keep scraping paused pending exit criteria. | Makes the narrow emergency procedure repeatable. | Unassigned | Proposed | Staging drill completes without touching an unrelated target. |
| P1 | Mitigate | Add a bounded emergency configuration that disables expensive application metrics while retaining probes and minimal operational metrics. | Preserves basic operability during exporter containment. | Unassigned | Proposed | Staging exercise disables only the intended families and keeps liveness/readiness observable. |
| P0 | Mitigate | Document that restarting only the container may preserve a pod `emptyDir`; pod replacement was required here. | Avoids ineffective restart loops. | Unassigned | Proposed | Runbook review and staging restart demonstrate the storage lifecycle. |
| P0 | Mitigate | Define a safe, verified procedure to restore the ServiceMonitor label after deploying the fix. | Prevents accidental early restoration or broad monitoring changes. | Unassigned | Proposed | Procedure validates exact target, fixed image, budgets, and stability window. |
| P1 | Mitigate | Continue shared-state/HA work tracked by #1569, without treating replicas as a standalone mitigation while authoritative state is memory-backed. | Reduces single-process amplification only after correctness permits HA. | Unassigned | Proposed | State-safety criteria pass before a multi-replica availability exercise. |
| P0 | Mitigate | After fixing cardinality, run a staging soak with scraping enabled and adversarial unique-path traffic. | Proves the complete fix under the triggering workload shape. | Unassigned | Proposed | Sustained soak meets every restoration criterion below. |

## Post-incident closeout

### Current state

- **Application:** healthy after replacement of the OOM-looping pod.
- **Telemetry:** token.place application scraping intentionally paused.
- **Incident:** mitigated, not resolved.
- **Code/deployment:** no permanent cardinality fix is claimed in this record, and the mitigation did
  not roll back or change the Deployment image.

### Required restoration exit criteria

Production application scraping must not be restored until all of the following are demonstrated:

1. Unmatched paths map to a bounded label set.
2. Thousands of unique paths do not grow Prometheus series linearly.
3. `/metrics` sample count, response size, and latency remain within explicit budgets.
4. Relay working set remains comfortably below the configured memory limit during a sustained
   scrape-and-traffic test.
5. An application-container restart cannot inherit stale multiprocess metric files.
6. The corrected image is deployed and its identity is verified.
7. Public health and application functionality remain healthy.
8. The ServiceMonitor is restored deliberately and observed through a defined stability window.

Pod replacement, permanently disabled scraping, a larger limit, edge filtering, or additional
replicas cannot substitute for these criteria or for bounded labels.

## Evidence gaps and unknowns

- The evidence window begins at 23:20 UTC, so the exact first unmatched request that began the
  buildup is unknown.
- Raw historical request paths and exact source addresses are unavailable or intentionally excluded
  from this public record. The traffic source cannot be attributed.
- Exact customer request failures and any in-memory work losses were not measured. There is an
  unquantified state-loss risk, not proven customer data loss.
- Exact PagerDuty trigger and acknowledgement timestamps are unavailable in the sanitized evidence.
- Pre-mitigation `kubectl top` and cgroup capture failed after the old container became unavailable.
- `container_oom_events` remained zero; this does not negate Kubernetes's authoritative
  `lastState.terminated.reason=OOMKilled` evidence.
- `scrape_body_bytes` remained zero; this does not mean the response was empty. Sanitized access-log
  response sizes are used instead.
- Thirty-second Prometheus samples may miss the instantaneous memory allocation that crossed the
  limit.
- No raw URLs, query strings, source addresses, request IDs, credentials, tokens, ciphertext,
  prompts, responses, tool data, node public keys, screenshots, or raw log archives are published
  with this record.

## Verification commands and public references

Repository inspection and documentation validation used non-production sources and commands only.
No Kubernetes, Prometheus, PagerDuty, Cloudflare, or other production system was accessed or
mutated while preparing this record.

Public references:

- [Deployed commit `e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22)
- [PR #1735: build-info deployment tag labels](https://github.com/futuroptimist/token.place/pull/1735)
- [PR #1726: separate Valkey scheduler/reservation/enqueue work](https://github.com/futuroptimist/token.place/pull/1726)
- [Issue #1569: separate shared-state/HA resilience work](https://github.com/futuroptimist/token.place/issues/1569)
- [Prometheus Python client multiprocess documentation](https://prometheus.github.io/client_python/multiprocess/)
- [Prometheus metric and label naming guidance](https://prometheus.io/docs/practices/naming/)

Local validation commands for this documentation-only change:

```text
python -m json.tool outages/2026-09-02-production-relay-metrics-cardinality-oom.json
python -m jsonschema -i outages/2026-09-02-production-relay-metrics-cardinality-oom.json outages/schema.json
pre-commit run --all-files
git diff --check
detect-secrets scan outages/2026-09-02-production-relay-metrics-cardinality-oom.md outages/2026-09-02-production-relay-metrics-cardinality-oom.json
```
