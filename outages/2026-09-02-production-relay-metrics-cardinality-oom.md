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

On September 2, 2026, the sole production token.place relay entered an OOM-restart loop. Default
Flask request instrumentation retained raw or effectively unbounded HTTP path values as Prometheus
labels. A rapid increase in distinct unmatched paths grew the application's Flask metric set from
roughly 29,000 to roughly 71,000 series. Collecting and serializing that multiprocess metric set on
each 30-second scrape increased `/metrics` response size and latency while relay memory approached
its 256Mi hard limit. Kubernetes then terminated the process as `OOMKilled` with exit code 137.

The multiprocess metric database was under `/tmp`, a pod-level `emptyDir`. It survived ordinary
application-container restarts in the same pod, preserving the high-cardinality state and sustaining
the crash loop. With one relay replica, an OOM interval could remove the only backend; browsers then
saw Traefik's `no available server` response. Healthy intervals occurred between crashes, so this
was intermittent unavailability during an approximately 87-minute crash loop, not 87 minutes of
continuous downtime.

At 2026-09-03 01:07:50 UTC, the operator paused discovery of only the token.place metrics target and
deleted only the affected pod. Kubernetes created a replacement with a fresh pod-level `emptyDir`;
it became healthy at 01:08:18 UTC with no image rollback or Deployment change. Application traffic
recovered, but application scraping remains intentionally paused. The incident is therefore
**Mitigated**, not resolved: a bounded-cardinality implementation must be deployed and pass the
restoration criteria below before scraping is restored.

## Impact

- **Verified:** Production was intermittently unavailable during the approximately 87-minute
  crash loop. The Deployment and Service reported an available endpoint during some healthy
  intervals, so the record does not claim continuous downtime.
- **Verified:** While the sole relay pod was unavailable, the browser-visible symptom was Traefik's
  `no available server` response.
- **Verified:** Repeated process termination occurred while API-v1 correctness state was still
  memory-backed.
- **Risk, not a confirmed outcome:** Those terminations created an unquantified risk of losing
  active in-memory relay state. Available evidence does not prove customer data or work loss.
- **Unknown:** Affected-user counts, failed-request counts, revenue impact, and the exact number of
  in-memory operations affected were not measured and are not estimated here.

Severity is **Critical** because a production PagerDuty alert accompanied user-visible
unavailability of the only relay replica.

## Detection

The incident was surfaced by a production PagerDuty alert and user-visible unavailability.
Kubernetes history provides the authoritative failure classification: the previous container state
was `OOMKilled`, exit code 137. The first historical restart and
`last_termination_oom` signals appeared at approximately 23:41 UTC, and the Prometheus target was
down by 23:47 UTC.

The exact PagerDuty trigger and acknowledgement timestamps are absent from the sanitized evidence,
so this record does not invent a page-fire time. The `container_oom_events` query remained zero and
is not used to negate Kubernetes's termination evidence. Likewise, `scrape_body_bytes` remained
zero; application access logs, rather than that query, establish the `/metrics` response size.

## Timeline

All incident times are shown in PDT and UTC. Approximate times are marked explicitly.

| Time (PDT) | Time (UTC) | Evidence status | Event |
| --- | --- | --- | --- |
| 2026-08-27 11:10:41 | 2026-08-27 18:10:41 | Verified | The ServiceMonitor was created. It remained generation 1 before mitigation and was already scraping; it was not newly activated at incident onset. |
| 2026-08-29 21:02:41 | 2026-08-30 04:02:41 | Verified | PR [#1735](https://github.com/futuroptimist/token.place/pull/1735) produced deployed commit [`e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22). Its relevant change was limited to immutable build-info labels. |
| 2026-09-01 20:53:30 | 2026-09-02 03:53:30 | Verified | PR [#1726](https://github.com/futuroptimist/token.place/pull/1726) merged as an internal, non-runtime-wired Valkey scheduler/reservation/enqueue slice. It was not in the deployed image and was unrelated to this outage. |
| 2026-09-02 16:20:00 | 2026-09-02 23:20:00 | Verified | The sanitized 30-second-resolution Prometheus evidence window begins. Relay working set was 197,107,712 bytes. |
| 2026-09-02 16:30:00 | 2026-09-02 23:30:00 | Verified | Working set was 197,111,808 bytes and RSS was 187,510,784 bytes. There were 1,679 distinct HTTP path labels, 1,659 unknown paths, 28,964 Flask series, and 28,996 scraped samples. Scrape duration was 3.102 seconds; the target was up. |
| 2026-09-02 16:39:30 | 2026-09-02 23:39:30 | Verified | Maximum sampled working set was 247,996,416 bytes: `247,996,416 / 268,435,456 × 100 = 92.386%`, approximately 92.4% of the 256Mi limit. |
| 2026-09-02 16:40:00 | 2026-09-02 23:40:00 | Verified | Maximum sampled RSS was 242,376,704 bytes. Path labels reached 2,741 (2,719 unknown), Flask series 47,018, and scraped samples 47,050. Scrape duration was 6.150 seconds and `scrape_series_added` was 2,261; the target was still up. |
| Approximately 2026-09-02 16:41 | Approximately 2026-09-02 23:41 | Verified | First historical restart and first `last_termination_oom` signal appeared. `scrape_series_added` reached its observed maximum of 2,822. The 30-second samples may not include the instantaneous allocation that crossed the limit. |
| 2026-09-02 16:46:30 | 2026-09-02 23:46:30 | Verified | Distinct path labels reached 4,124, including 4,093 unknown paths; Flask series reached 70,529. |
| By 2026-09-02 16:47 | By 2026-09-02 23:47 | Verified | The Prometheus target was down and the restart counter was 2. |
| 2026-09-02 16:48:00 | 2026-09-02 23:48:00 | Verified | Observed maxima were 4,155 path labels, 4,121 unknown paths, 71,056 Flask series, and 71,088 scraped samples. |
| 2026-09-02 16:55:00 | 2026-09-02 23:55:00 | Verified | Restart counter reached 5. |
| 2026-09-02 17:05:00 | 2026-09-03 00:05:00 | Verified | Restart counter reached 7. |
| 2026-09-02 17:50:09 | 2026-09-03 00:50:09 | Verified | Kubernetes recorded the previous container termination as `OOMKilled`, exit code 137. |
| 2026-09-02 18:07:50 | 2026-09-03 01:07:50 | Verified | After validating the exact context and target, the operator paused only token.place ServiceMonitor discovery and deleted only the OOM-looping pod. The image and Deployment were unchanged. |
| 2026-09-02 18:08:00 | 2026-09-03 01:08:00 | Verified | The historical Prometheus evidence window ends. The restart counter's pre-mitigation maximum was 16. |
| 2026-09-02 18:08:18 | 2026-09-03 01:08:18 | Verified | Replacement pod `tokenplace-7758c45ffb-fr45t` started and became healthy with a fresh `emptyDir`. Subsequent verification found zero restarts and HTTP 200 on `/livez`, `/healthz`, and `/`. |

During the crash loop, sanitized application access logs also recorded successful `/metrics`
responses of approximately 7,788,590–7,788,593 bytes taking approximately 6.45–7.93 seconds. The
maximum observed Prometheus scrape duration was 8.238 seconds. Kubernetes recorded 251 `BackOff`
events over approximately 73 minutes. The affected pod had reached 15 restarts during initial triage
and 16 in the later historical query.

## Technical root cause

### Confirmed mechanism

The latent application defect was unbounded metric-label cardinality:

1. Default Flask request instrumentation retained raw or effectively unbounded request paths as
   Prometheus label values.
2. Distinct path values rose from 1,679 at 23:30 UTC to a maximum of 4,155 at 23:48 UTC. Unknown or
   unmatched routes accounted for 1,659 and 4,121 respectively—nearly all values at both points.
3. Flask series consequently grew from 28,964 to 71,056, while total scraped samples grew from
   28,996 to 71,088.
4. Every 30-second scrape collected and serialized the large multiprocess metric set. Successful
   responses reached approximately 7.79 MB and 6.45–7.93 seconds; observed scrape duration reached
   8.238 seconds.
5. Relay working set reached a sampled 247,996,416 bytes, approximately 92.4% of the 268,435,456-byte
   limit, immediately before the first historical OOM/restart signal. Kubernetes then OOM-killed
   the process. Sampling does not need to capture the instantaneous over-limit allocation for the
   Kubernetes termination reason to be authoritative.
6. `PROMETHEUS_MULTIPROC_DIR` pointed to `/tmp`, mounted from a pod-level `emptyDir`. Ordinary
   container restarts within the same pod retained the accumulated metric database, so restarts did
   not remove the trigger state and the loop continued.
7. The Deployment had one relay replica; every termination could temporarily leave the Service
   without a serving backend.

This was not evidence that Prometheus leaked memory. The application's unbounded label state and
the work required to collect and serialize it drove memory pressure against a hard limit.

### Trigger attribution

**Supported inference:** The rapid appearance of thousands of distinct unmatched paths is
consistent with automated scanning or randomized-path traffic.

**Unresolved:** Raw historical paths and complete access logs were not retained in the public
evidence, and exact source addresses are unavailable or intentionally excluded. The traffic cannot
be attributed to an attacker, crawler, customer, or other exact source. Automated scanning is a
plausible trigger, not a verified identity or cause. Unusual traffic exposed the latent unbounded-
cardinality defect; it did not create the defect.

### Deployment-change exclusion and PR #1726

**Verified:** Production ran
`ghcr.io/futuroptimist/tokenplace-relay:sha-e46277d`. Commit `e46277d` was created on August 30,
before PR #1726 merged on September 2 at 03:53:30 UTC. The deployed commit's incident-relevant
change was limited to immutable build-info labels from PR #1735. No production rollout occurred
near incident onset; the ReplicaSet and pod were approximately three days old.

PR #1726's 65 commits were an internal, non-runtime-wired Valkey scheduler, reservation, and enqueue
slice. They were not present in the deployed image and did not cause this outage. PR #1726 and the
separate shared-state/HA work in [#1569](https://github.com/futuroptimist/token.place/issues/1569)
are relevant only to future resilience, not trigger attribution.

## Contributing factors

- Raw or effectively unbounded paths were available to default request metrics instead of bounded
  route templates or endpoint names.
- There was no regression/load gate proving that thousands of unique 404 paths created only a
  fixed number of series.
- Cardinality, samples, scrape latency, response size, and memory headroom were not presented
  together with actionable budgets and alerts.
- The 256Mi hard limit left little headroom for collection and serialization once the application
  metric set had expanded. Raising it alone would only delay failure and is not a root fix.
- Multiprocess metrics shared the general `/tmp` pod `emptyDir` and were not safely cleared on each
  application-container startup.
- A container restart retained the problematic files, making automatic restart ineffective.
- A single replica amplified each process failure into possible loss of the only serving endpoint.
  Adding replicas is not currently a safe standalone mitigation while authoritative relay state is
  memory-backed.
- The metrics target alert detected a downstream symptom rather than warning first on cardinality,
  memory headroom, OOM state, or restart acceleration.

## Recovery and resolution

At 2026-09-03 01:07:50 UTC, the operator first verified context `sugar-prod`, namespace and
Deployment `tokenplace`, one desired and available replica, image
`ghcr.io/futuroptimist/tokenplace-relay:sha-e46277d`, the 256Mi limit, and the ServiceMonitor
configuration. The affected pod was `tokenplace-7758c45ffb-dqccb`, UID
`e151eed5-6ecf-466e-9cc2-79956ea71903`.

The operator then:

1. Changed only the ServiceMonitor `release` label from `kube-prometheus-stack` to
   `incident-paused-tokenplace-oom`, pausing Prometheus discovery only for token.place.
2. Deleted only the exact OOM-looping pod, causing Kubernetes to create a new pod and a fresh
   pod-level `emptyDir`.
3. Did not roll back the image and did not change the Deployment.

Replacement pod `tokenplace-7758c45ffb-fr45t`, UID
`2d1a6ad9-aff9-40d9-a3f3-60ed062c387b`, started on node `sugarkube0` at
2026-09-03 01:08:18 UTC. This emergency action restored service by stopping expensive scrapes and
discarding the accumulated pod-persistent metric files. It is mitigation, not the permanent fix.

Application state is healthy; telemetry state is intentionally degraded because token.place
application scraping remains paused. The ServiceMonitor must not be re-enabled until the permanent
fix satisfies every exit criterion below.

## Post-recovery verification

The replacement pod reported `Ready=True` and zero restarts during post-mitigation verification.
Public requests returned HTTP 200 for `/livez`, `/healthz`, and `/`. The replacement identity, new
UID, node, start time, unchanged deployed image, and unchanged Deployment were also verified.

These checks established recovery of application availability, not resolution of the exporter
defect. Since normal application scraping is paused, post-recovery health does not prove that the
old scrape workload is safe.

## What went well

- Operators verified the exact production context, workload, image, replica count, limit, and
  monitoring target before applying a narrowly scoped mitigation.
- Kubernetes termination state clearly identified `OOMKilled` despite an unhelpful
  `container_oom_events` query.
- Historical 30-second metrics and sanitized aggregate access logs made the cardinality, scrape,
  memory, and restart sequence reconstructable without publishing raw paths or payloads.
- Pausing only the affected ServiceMonitor avoided disabling unrelated monitoring.
- Replacing the pod cleared the pod-persistent multiprocess database without changing or rolling
  back the Deployment image.
- Public health checks and replacement-pod status confirmed that application service recovered.

## What went poorly

- Caller-controlled path diversity could create application metric series without a fixed bound.
- A routine scrape expanded into multi-megabyte, multi-second work inside a memory-constrained
  application process.
- Restarting the application container preserved the high-cardinality multiprocess files, allowing
  the loop to continue.
- A single replica made every crash capable of removing the only production backend.
- Monitoring did not provide sufficiently early, correlated cardinality and memory warnings.
- Direct pre-mitigation `kubectl top` and cgroup capture failed because the old container was
  already unavailable.
- Sanitized evidence cannot establish the exact traffic source, the first triggering path, exact
  customer failures, or whether active memory-backed work was lost.

## Corrective actions

No permanent corrective action is claimed complete. Owners and tracking identifiers must be
assigned separately rather than invented in this record.

| Priority | Type | Action | Rationale | Owner | Status | Verification or exit criterion |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | Prevent | Replace raw-path Flask grouping with bounded route-template or endpoint labels. | Removes the root unbounded-cardinality defect. | Unassigned | Proposed | Enumerated application routes produce a fixed label set. |
| P0 | Prevent | Collapse every unmatched/404 route to one fixed label; prohibit query strings, request IDs, model names, keys, tokens, and arbitrary segments in labels. | Prevents attacker- or caller-controlled label growth and sensitive label content. | Unassigned | Proposed | Thousands of diverse 404s produce one unmatched-route value and no caller data appears in exposition. |
| P1 | Prevent | Prefer a small, explicitly registered application metric set over implicit default per-path instrumentation. | Makes cardinality reviewable and budgetable. | Unassigned | Proposed | Metric and label allowlists are documented and tested. |
| P0 | Prevent | Add a load regression sending thousands of unique unmatched paths and budget series count, scrape output size, scrape duration, and memory. | Reproduces the incident trigger safely and blocks recurrence. | Unassigned | Proposed | Test remains within fixed, review-approved budgets under sustained scrapes. |
| P0 | Prevent | Move multiprocess metrics to a dedicated directory and safely clear it on every application-container startup before Gunicorn launches. | Container restart must not inherit stale series; pod deletion cannot be the cleanup mechanism. | Unassigned | Proposed | Restart test proves old metric files and series are absent before workers start. |
| P1 | Prevent | Validate Prometheus multiprocess worker cleanup. | Prevents dead-worker files from accumulating. | Unassigned | Proposed | Worker churn test demonstrates bounded files and correct exposition. |
| P2 | Prevent | Consider bounded edge rate limits or scanner controls for unmatched paths. | Defense in depth reduces waste but cannot replace bounded labels. | Unassigned | Proposed | Control is tested without treating filtered traffic as the cardinality fix. |
| P2 | Prevent | Reassess the 256Mi limit only after measuring the corrected exporter. | Measured headroom is useful; a temporary increase only delays the latent failure. | Unassigned | Proposed | Limit follows a documented corrected-exporter load profile and safety margin. |
| P0 | Detect | Alert on relay working-set-to-limit ratio with warning and critical thresholds. | Provides warning before hard-limit termination. | Unassigned | Proposed | Controlled load fires both thresholds before OOM. |
| P0 | Detect | Alert directly on `OOMKilled` state and restart acceleration. | Detects failure without waiting for the metrics target to go down. | Unassigned | Proposed | Synthetic restart/OOM fixtures exercise routing and alert transitions. |
| P1 | Detect | Establish per-target budgets and alerts for `scrape_samples_scraped`, `scrape_series_added`, `scrape_duration_seconds`, and distinct bounded route labels. | Detects cardinality and scrape-cost drift. | Unassigned | Proposed | Threshold tests fire on budget breach and stay quiet at accepted load. |
| P1 | Detect | Add a dashboard correlating metric cardinality, scrape size/sample count, scrape latency, memory headroom, and restarts. | Makes the causal sequence visible during triage. | Unassigned | Proposed | Staging drill displays all signals over one time range. |
| P0 | Detect | Add a release/staging gate that fails when unique unknown paths create unbounded series. | Prevents recurrence from reaching production. | Unassigned | Proposed | Adversarial unique-path test is required and fails an intentionally unbounded fixture. |
| P1 | Detect | Retain privacy-safe aggregate access evidence long enough to classify cardinality triggers. | Improves attribution without retaining credentials, query strings, encrypted payloads, or sensitive paths. | Unassigned | Proposed | Retention and field allowlist pass privacy/security review. |
| P0 | Mitigate | Write a metrics-induced OOM-loop runbook: validate context/target, pause only the affected monitor, replace the pod, verify public health, and keep scraping paused pending exit criteria. | Makes the successful narrow recovery repeatable and safe. | Unassigned | Proposed | Staging exercise completes without touching unrelated targets. |
| P1 | Mitigate | Add a bounded emergency mode disabling expensive application metrics while retaining probes and minimal operational metrics. | Preserves essential visibility and availability during exporter incidents. | Unassigned | Proposed | Staging drill disables only expensive metrics and leaves probes/minimal metrics healthy. |
| P0 | Mitigate | Document that container restart may retain a pod `emptyDir` and that this incident required pod replacement. | Prevents ineffective restart-only recovery. | Unassigned | Proposed | Runbook explains storage lifetime and exercise confirms fresh state after replacement. |
| P0 | Mitigate | Define a safe procedure to restore the ServiceMonitor label after the corrected deployment. | Prevents accidental early restoration or broad monitoring changes. | Unassigned | Proposed | Procedure verifies target identity and completes the stability window below. |
| P1 | Mitigate | Continue shared-state/HA work tracked by [#1569](https://github.com/futuroptimist/token.place/issues/1569). Do not treat replicas as a standalone mitigation while authoritative state is memory-backed. | Reduces single-replica amplification only after correctness permits safe HA. | Unassigned | Proposed | Shared-state correctness and failover tests pass before replica count is used as mitigation. |
| P0 | Mitigate | After the fix, run a staging soak with scraping enabled and adversarial unique-path traffic. | Proves the complete fixed path before restoring production scraping. | Unassigned | Proposed | All restoration criteria below hold throughout the approved soak. |

## Post-incident closeout

The incident remains **Mitigated**. Production application scraping must not be restored until all
of the following are demonstrated:

- unmatched paths map to a bounded label set;
- thousands of unique paths do not grow Prometheus series linearly;
- `/metrics` sample count, response size, and latency remain within explicit, review-approved
  budgets;
- relay working set remains comfortably below the configured memory limit during sustained scrape
  and adversarial traffic testing;
- an application-container restart cannot inherit stale multiprocess metric files;
- the corrected image is deployed and its identity is verified;
- public health and application functionality remain healthy; and
- the ServiceMonitor is restored deliberately and observed through a defined stability window.

Closeout requires recording the approved budgets, staging-soak result, corrected image identity,
production restoration time, and stability-window result. Disabling scraping permanently, raising
memory, replacing a pod, filtering traffic, or adding replicas alone cannot close the incident.

## Evidence gaps and unknowns

- The historical evidence window begins at 23:20 UTC, so the first unmatched request that began the
  buildup is unknown.
- Raw historical request paths and exact source addresses are unavailable or intentionally
  excluded. The traffic source cannot be attributed.
- Exact customer request failures and any in-memory work losses were not measured. The latter is an
  unquantified risk, not proven data loss.
- Exact PagerDuty trigger and acknowledgement timestamps are unavailable in the sanitized evidence.
- Direct pre-mitigation `kubectl top` and cgroup capture failed after the old container became
  unavailable.
- `container_oom_events` remained zero, but Kubernetes `lastState.terminated.reason=OOMKilled` is
  authoritative. The zero is not evidence that no OOM occurred.
- `scrape_body_bytes` remained zero, but application access-log sizes establish successful
  multi-megabyte responses. The zero is not evidence of an empty response.
- Thirty-second memory samples may have missed the instantaneous allocation that crossed the limit.
- Raw paths, query strings, source addresses, request identifiers, credentials, keys, ciphertext,
  prompts, responses, tool data, and arbitrary payloads are intentionally outside this public
  record's privacy boundary.

## Verification commands and public references

Repository references reviewed for this record:

- [`relay.py`](../relay.py), including the deployed Flask/Prometheus instrumentation and `/metrics`
  behavior
- [`Dockerfile`](../Dockerfile), including the deployed `PROMETHEUS_MULTIPROC_DIR=/tmp`
- [`charts/tokenplace/templates/deployment.yaml`](../charts/tokenplace/templates/deployment.yaml),
  including resources and the `/tmp` `emptyDir`
- [`charts/tokenplace/templates/servicemonitor.yaml`](../charts/tokenplace/templates/servicemonitor.yaml)
- [`charts/tokenplace/values.yaml`](../charts/tokenplace/values.yaml)
- [`tests/unit/test_relay_logging_and_metrics.py`](../tests/unit/test_relay_logging_and_metrics.py)
- [`tests/unit/test_tokenplace_chart_metrics.py`](../tests/unit/test_tokenplace_chart_metrics.py)
- deployed commit [`e46277daaeb76beeb9f2a2e9e265181287239b22`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22)
- [PR #1735](https://github.com/futuroptimist/token.place/pull/1735)
- unrelated [PR #1726](https://github.com/futuroptimist/token.place/pull/1726)
- separate shared-state/HA [issue #1569](https://github.com/futuroptimist/token.place/issues/1569)
- [Prometheus Python client multiprocess documentation](https://prometheus.github.io/client_python/multiprocess/)

Documentation validation commands and results are recorded in the companion pull request. This
public record contains sanitized aggregate evidence only; it intentionally excludes production
system access, private evidence locations, screenshots, raw logs, and diagnostic archives.
