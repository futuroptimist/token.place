# Outage: production relay metrics-cardinality OOM crash loop

## Incident metadata

- **Date**: 2026-09-02
- **Severity**: Critical
- **Status**: Mitigated
- **Component**: production token.place relay and its application metrics endpoint
- **Incident ID**: `2026-09-02-production-relay-metrics-cardinality-oom`
- **Primary incident window**: approximately 2026-09-02 16:41 PDT / 23:41 UTC through
  2026-09-02 18:08:18 PDT / 2026-09-03 01:08:18 UTC

## Summary

On September 2, 2026, the sole production token.place relay entered an approximately 87-minute
crash loop. During each unavailable interval, Traefik could return `no available server` because
there was no second relay backend. Kubernetes identified the terminated application process as
`OOMKilled` with exit code 137.

The confirmed root cause was default Flask request instrumentation that retained raw or effectively
unbounded HTTP path values in Prometheus labels. A rapid increase in distinct unmatched paths grew
the Flask metric set from about 29,000 to about 71,000 series. Each 30-second scrape collected and
serialized that multiprocess metric set; successful `/metrics` responses reached approximately
7.79 MB and took 6.45–7.93 seconds in application access logs. Relay memory approached its 256Mi
hard limit and the process was OOM-killed. Because `PROMETHEUS_MULTIPROC_DIR=/tmp` used a pod-level
`emptyDir`, ordinary application-container restarts retained the accumulated metric database and
the crash loop continued.

At 2026-09-03 01:07:50 UTC, an operator paused Prometheus discovery only for the token.place target
and deleted only the affected pod. Kubernetes created a replacement pod with a fresh `emptyDir`;
the replacement became healthy at 01:08:18 UTC with zero restarts, and public health and application
checks returned HTTP 200. No image rollback or Deployment change was made.

The incident remains **Mitigated**, not resolved. Production application scraping is intentionally
paused. It must not be restored until a bounded-cardinality fix, safe multiprocess cleanup, resource
budgets, and sustained verification meet the exit criteria below.

## Impact

- **Verified:** Production experienced intermittent, not continuous, unavailability during the
  approximately 87-minute crash loop. The Deployment and Service reported an available endpoint
  during some healthy intervals between crashes.
- **Verified:** When the single relay pod was unavailable, the browser-visible symptom was Traefik's
  `no available server` response.
- **Verified:** One desired replica and one available replica were configured before the failures;
  each crash could therefore temporarily remove the only serving endpoint.
- **Risk, not confirmed loss:** API-v1 correctness state was still memory-backed. Repeated process
  termination created an unquantified risk of losing active in-memory relay state. Available
  evidence does not establish that customer data or active work was actually lost.
- **Unknown:** Affected-user counts, failed-request counts, revenue impact, and the exact number of
  in-memory operations interrupted were not measured and are not estimated here.
- **Severity basis:** Critical reflects a production PagerDuty alert and user-visible production
  unavailability, not an inferred volume of affected users.

## Detection

The incident was surfaced through a production PagerDuty alert and user-visible unavailability.
The exact PagerDuty trigger and acknowledgement timestamps are absent from the sanitized evidence,
so this record does not invent a precise page-fire time.

Historical Prometheus evidence shows the target up at 23:30 and 23:40 UTC, the first restart and
`last_termination_oom` signal at 23:41 UTC, and the target down by 23:47 UTC. Kubernetes
`lastState.terminated.reason=OOMKilled` is the authoritative OOM evidence. The
`container_oom_events` series remained zero and must not be interpreted as evidence that no OOM
occurred.

## Timeline

All times are PDT (UTC−07:00) and UTC. Approximate times are marked explicitly.

| Time (PDT) | Time (UTC) | Evidence class | Event |
| --- | --- | --- | --- |
| 2026-08-27 11:10:41 | 2026-08-27 18:10:41 | Verified | The ServiceMonitor was created. It remained generation 1 before mitigation and was already actively scraping; it was not newly activated near incident onset. |
| 2026-08-29, approximately | 2026-08-30, approximately | Verified | The affected ReplicaSet/pod generation began running image `ghcr.io/futuroptimist/tokenplace-relay:sha-e46277d`. The workload was approximately three days old at the incident; no rollout occurred near onset. |
| 2026-09-02 16:20:00 | 2026-09-02 23:20:00 | Verified | The retained 30-second Prometheus evidence window begins. Working set was 197,107,712 bytes. Evidence before this point is unavailable. |
| 2026-09-02 16:30:00 | 2026-09-02 23:30:00 | Verified | Working set was 197,111,808 bytes and RSS was 187,510,784 bytes. There were 1,679 distinct HTTP path labels, 1,659 unmatched-path labels, 28,964 Flask series, and 28,996 scraped samples. Scrape duration was 3.102 seconds; the target was up. |
| 2026-09-02 16:39:30 | 2026-09-02 23:39:30 | Verified | Maximum sampled working set was 247,996,416 bytes: `247,996,416 / 268,435,456 × 100 = 92.385...%`, or approximately 92.4% of the 256Mi limit. |
| 2026-09-02 16:40:00 | 2026-09-02 23:40:00 | Verified | Maximum sampled RSS was 242,376,704 bytes. Distinct path labels reached 2,741, including 2,719 unmatched paths; Flask series reached 47,018 and scraped samples 47,050. Scrape duration was 6.150 seconds, `scrape_series_added` was 2,261, and the target remained up. |
| Approximately 2026-09-02 16:41 | Approximately 2026-09-02 23:41 | Verified | The first historical restart and first `last_termination_oom` signal appeared. `scrape_series_added` reached its observed maximum of 2,822. The 30-second memory samples may not include the instantaneous allocation that crossed the hard limit. |
| 2026-09-02 16:46:30 | 2026-09-02 23:46:30 | Verified | Distinct paths reached 4,124, including 4,093 unmatched paths; Flask series reached 70,529. |
| 2026-09-02 16:47 | 2026-09-02 23:47 | Verified | Prometheus showed the target down and the restart counter at 2. |
| 2026-09-02 16:48 | 2026-09-02 23:48 | Verified | Observed maxima reached 4,155 distinct paths, 4,121 unmatched paths, 71,056 Flask series, and 71,088 scraped samples. |
| 2026-09-02 16:55 | 2026-09-02 23:55 | Verified | The restart counter reached 5. |
| 2026-09-02 17:05 | 2026-09-03 00:05 | Verified | The restart counter reached 7. |
| 2026-09-02 17:50:09 | 2026-09-03 00:50:09 | Verified | Kubernetes recorded the affected container's prior termination as `OOMKilled`, exit code 137. |
| Before mitigation | Before mitigation | Verified | Pod `tokenplace-7758c45ffb-dqccb` had 15 restarts at initial triage and 16 in the later historical query. Events included 251 BackOff events over approximately 73 minutes. |
| 2026-09-02 18:07:50 | 2026-09-03 01:07:50 | Verified | After confirming the exact target and workload, the operator changed only the ServiceMonitor `release` label from `kube-prometheus-stack` to `incident-paused-tokenplace-oom`, then deleted only the exact OOM-looping pod. The image and Deployment were unchanged. |
| 2026-09-02 18:08:18 | 2026-09-03 01:08:18 | Verified | Replacement pod `tokenplace-7758c45ffb-fr45t` started on `sugarkube0`, became ready, and ended the confirmed crash-loop window. |
| After 2026-09-02 18:08:18 | After 2026-09-03 01:08:18 | Verified | The replacement remained ready with zero restarts during verification. `/livez`, `/healthz`, and `/` returned HTTP 200. Application scraping remained intentionally paused. |

## Technical root cause

### Confirmed failure mechanism

Verified facts establish this chain:

1. Default Flask request instrumentation retained raw or effectively unbounded HTTP paths as label
   values. Nearly all path-label values in the observed growth were unknown/unmatched routes.
2. Distinct path values rose from 1,679 at 23:30 UTC to 4,155 at 23:48 UTC; unmatched values rose
   from 1,659 to 4,121. Flask metric series rose from 28,964 to 71,056.
3. Prometheus scraped `/metrics` every 30 seconds. Each scrape collected and serialized the large
   application-owned multiprocess metric set. This is not evidence of a Prometheus memory leak.
4. Application logs recorded successful `/metrics` bodies of approximately
   7,788,590–7,788,593 bytes and durations of approximately 6.45–7.93 seconds. Historical metrics
   show scrape duration increasing from 3.102 seconds at 23:30 to 6.150 seconds at 23:40, with an
   observed maximum of 8.238 seconds.
5. Relay working set reached a sampled 247,996,416 bytes, approximately 92.4% of the
   268,435,456-byte limit. The next transient allocation need not appear in 30-second sampling.
   Kubernetes subsequently recorded an OOM kill.
6. `PROMETHEUS_MULTIPROC_DIR` pointed to `/tmp`, mounted from a pod-level `emptyDir`. Container
   restarts within the same pod did not clear that storage, so accumulated metric files remained
   available to the restarted process and the failure repeated.
7. With only one replica, an OOM interval could leave the Service without a serving backend.

The `scrape_body_bytes` query remained zero and is not used to infer response size; the response-size
evidence comes from sanitized application access-log aggregates. Direct pre-mitigation cgroup and
`kubectl top` collection failed because the old container was already unavailable.

### Trigger attribution

**Supported inference:** The rapid arrival of thousands of unmatched, distinct paths is consistent
with automated scanning or randomized-path traffic.

**Unresolved:** Raw historical paths and complete access logs were not retained in the public
evidence. No attacker, crawler, customer, or exact traffic source can be identified. Unusual traffic
exposed the defect; the latent application defect was unbounded label cardinality, and filtering a
suspected traffic class would not correct it.

### Deployment and change attribution

The production context was verified as `sugar-prod`; the namespace and Deployment were both
`tokenplace`. Production ran one desired and available replica with image
`ghcr.io/futuroptimist/tokenplace-relay:sha-e46277d` and a 256Mi memory limit. The affected pod was
`tokenplace-7758c45ffb-dqccb`, UID `e151eed5-6ecf-466e-9cc2-79956ea71903`.

[PR #1735](https://github.com/futuroptimist/token.place/pull/1735) produced deployed commit
[`e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22)
on August 30. Its relevant runtime change was limited to immutable build-info labels. There was no
production rollout near incident onset; the ReplicaSet/pod was approximately three days old.

[PR #1726](https://github.com/futuroptimist/token.place/pull/1726) was unrelated to this outage. It
merged on 2026-09-02 at 03:53:30 UTC as an internal, non-runtime-wired Valkey
scheduler/reservation/enqueue slice. Production was still running `sha-e46277d`, a commit created
before #1726 merged. None of #1726's 65 commits were in that deployed image, and this record does not
attribute the incident to them.

## Contributing factors

- Default instrumentation admitted caller-controlled path diversity into metric labels.
- No regression or staging load gate tested thousands of distinct unmatched paths against fixed
  series, output-size, latency, and memory budgets.
- Cardinality, series-addition rate, scrape duration, and memory headroom were not presented and
  alerted together early enough to prevent the OOM.
- The 256Mi limit provided little headroom once the exporter state and serialization cost grew. A
  larger limit could delay failure but would not bound label growth or fix the defect.
- Multiprocess metrics shared general-purpose pod `/tmp`; container restart lifecycle and metrics
  file cleanup lifecycle did not align.
- A container restart preserved the pod `emptyDir`, allowing the high-cardinality state to survive.
- The single replica amplified one process failure into possible total backend unavailability.
- Adding replicas is not yet a safe standalone mitigation because authoritative API-v1 correctness
  state remains memory-backed; separate shared-state/HA work is tracked by
  [#1569](https://github.com/futuroptimist/token.place/issues/1569).
- Scanner controls were not the primary safeguard. Such controls are defense in depth and cannot
  substitute for bounded application labels.

## Recovery and resolution

At 01:07:50 UTC, the operator first verified the exact production context, namespace, Deployment,
image, replica count, memory limit, and ServiceMonitor configuration. The operator then:

1. paused discovery of only the token.place application target by changing the ServiceMonitor's
   `release` label from `kube-prometheus-stack` to `incident-paused-tokenplace-oom`;
2. deleted only affected pod `tokenplace-7758c45ffb-dqccb`, forcing creation of a new pod and fresh
   pod-level `emptyDir`; and
3. left the Deployment and deployed image unchanged.

Replacement pod `tokenplace-7758c45ffb-fr45t`, UID
`2d1a6ad9-aff9-40d9-a3f3-60ed062c387b`, started on `sugarkube0` at 01:08:18 UTC. The application
recovered because expensive periodic scrapes were stopped and the persistent-within-pod metric
files were discarded with the old pod.

This was an emergency mitigation, not the permanent fix. Deleting a pod, increasing memory,
filtering traffic, adding replicas, or permanently disabling application telemetry does not remove
the unbounded-label defect. Status remains **Mitigated** while scraping is paused and the permanent
fix and restoration verification remain pending.

## Post-recovery verification

Verified replacement state:

- pod: `tokenplace-7758c45ffb-fr45t`;
- pod UID: `2d1a6ad9-aff9-40d9-a3f3-60ed062c387b`;
- node: `sugarkube0`;
- started: `2026-09-03T01:08:18Z`;
- ready: `True`;
- restarts during mitigation verification: `0`;
- `/livez`, `/healthz`, and `/`: HTTP 200;
- application state: healthy; and
- telemetry state: token.place application scraping intentionally paused.

These checks establish recovery from the crash loop, but not safe restoration of metrics scraping.

## What went well

- Kubernetes termination state gave authoritative OOM evidence even though one OOM metric was zero.
- Historical 30-second metrics correlated cardinality, scrape cost, memory pressure, target state,
  and restarts without publishing sensitive request content.
- The operator verified the production context and exact resource before making a narrow change.
- Mitigation paused only the affected monitoring target and replaced only the affected pod; it did
  not roll back an unrelated image or change the Deployment.
- Public health and application checks confirmed that serving recovered immediately after the new
  pod started.

## What went poorly

- Caller-controlled unmatched paths could create application metric series without a fixed bound.
- Scraping that state became large and slow enough to compete with the relay under a tight memory
  limit.
- Ordinary container restarts retained multiprocess files, making self-recovery ineffective.
- A single replica allowed each crash to remove the only backend.
- Existing alerting did not provide an earlier combined view of cardinality growth, memory headroom,
  scrape cost, OOM state, and restart acceleration.
- Publicly safe aggregate evidence was insufficient to determine the exact traffic source or count
  customer impact.

## Corrective actions

No repository issue number or assignee is implied below. Unless noted, each action is **Proposed**
and **Unassigned**.

| Priority | Type | Action | Rationale | Owner | Status | Verification or exit criterion |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | Prevent | Replace raw-path Flask grouping with bounded route-template or endpoint labels. | Removes caller-controlled cardinality. | Unassigned | Proposed | A reviewed metric schema contains only an enumerated route set. |
| P0 | Prevent | Collapse all unmatched/404 routes to one fixed label; prohibit query strings, request IDs, model names, keys, tokens, and arbitrary segments as labels. | Prevents sensitive or unbounded values entering metrics. | Unassigned | Proposed | Automated tests verify one label for thousands of distinct unknown URLs and absence of prohibited values. |
| P0 | Prevent | Prefer a small, explicitly registered metric set over implicit default per-path instrumentation. | Makes label dimensions reviewable and bounded. | Unassigned | Proposed | `/metrics` exposes only the approved registry and label dimensions. |
| P0 | Prevent | Add a regression/load test with thousands of unique unmatched paths and fixed series, output-size, scrape-duration, and memory budgets. | Reproduces the incident pressure safely. | Unassigned | Proposed | Test passes repeatedly in CI/staging without linear series growth. |
| P0 | Prevent | Move multiprocess metrics to a dedicated directory and clear it safely on every application-container startup before Gunicorn. | Aligns file lifetime with the application process rather than the pod. | Unassigned | Proposed | A container restart starts with no stale multiprocess files without requiring pod deletion. |
| P0 | Prevent | Validate Prometheus multiprocess worker cleanup. | Prevents dead-worker files and series from accumulating. | Unassigned | Proposed | Worker churn test proves correct cleanup and stable series count. |
| P2 | Prevent | Consider a bounded edge rate limit or scanner control for unmatched paths. | Adds defense in depth without replacing bounded labels. | Unassigned | Proposed | Legitimate traffic remains healthy and labels stay bounded even when the control is bypassed. |
| P2 | Prevent | Reassess the 256Mi limit only after measuring the corrected exporter. | Capacity should follow evidence; extra memory is not the root fix. | Unassigned | Proposed | Limit is justified by sustained measured peak plus documented headroom. |
| P0 | Detect | Alert on relay working-set-to-limit ratios at warning and critical thresholds. | Gives actionable warning before an OOM. | Unassigned | Proposed | Alert fires at tested thresholds and routes correctly. |
| P0 | Detect | Alert directly on `OOMKilled` state and restart acceleration. | Avoids waiting for only the scrape target to become unavailable. | Unassigned | Proposed | Synthetic OOM/restart fixtures exercise both alerts. |
| P1 | Detect | Define per-target budgets and alerts for `scrape_samples_scraped`, `scrape_series_added`, `scrape_duration_seconds`, and bounded route-label count. | Detects exporter growth and scrape stress. | Unassigned | Proposed | Staging threshold tests fire before resource exhaustion. |
| P1 | Detect | Add one dashboard for metric cardinality, samples/size, scrape latency, memory headroom, and restarts. | Makes the causal signals visible together. | Unassigned | Proposed | Dashboard is reviewed against a controlled cardinality test. |
| P0 | Detect | Add a release/staging gate that fails when unique unknown paths create unbounded series. | Blocks recurrence before production. | Unassigned | Proposed | Adversarial unique-path test is a required passing gate. |
| P1 | Detect | Retain privacy-safe aggregate access evidence long enough to classify future triggers, excluding credentials, query strings, encrypted payloads, and sensitive paths. | Improves attribution without weakening privacy. | Unassigned | Proposed | Privacy review and retention test confirm only bounded aggregates. |
| P0 | Mitigate | Write a metrics-induced OOM runbook: verify context/target, pause only its ServiceMonitor, replace the pod, verify public health, and keep scraping paused until exit criteria pass. | Makes the narrow emergency response repeatable. | Unassigned | Proposed | Tabletop exercise completes without affecting unrelated targets. |
| P1 | Mitigate | Add a bounded emergency switch that disables expensive application metrics while preserving health and minimal operational metrics. | Reduces reliance on changing discovery configuration. | Unassigned | Proposed | Drill shows serving and minimal health telemetry remain available. |
| P0 | Mitigate | Document that a container restart may retain pod `emptyDir` state and that this incident required pod replacement. | Prevents an ineffective restart loop. | Unassigned | Proposed | Runbook explicitly distinguishes container restart from pod replacement. |
| P0 | Mitigate | Define a safe, verified procedure to restore the ServiceMonitor label after the fix. | Prevents accidental early reactivation. | Unassigned | Proposed | Procedure includes identity checks, staged enablement, rollback, and observation. |
| P1 | Mitigate | Continue #1569 shared-state/HA work, without treating replicas as safe standalone mitigation while authoritative state is memory-backed. | Reduces single-process amplification without risking state divergence. | Unassigned | Proposed | HA is enabled only after authoritative shared-state correctness tests pass. |
| P0 | Mitigate | After the cardinality fix, run a staging soak with scraping enabled and adversarial unique-path traffic. | Demonstrates safe behavior under the incident trigger shape. | Unassigned | Proposed | All restoration exit criteria remain satisfied for the defined soak window. |

## Post-incident closeout

### Required restoration exit criteria

Production application scraping must not be restored until **all** of the following are demonstrated:

- unmatched paths map to a bounded label set;
- thousands of unique paths do not grow Prometheus series linearly;
- `/metrics` sample count, response size, and latency remain within explicit, reviewed budgets;
- relay working set remains comfortably below the configured memory limit during a sustained
  scrape-and-traffic test;
- a container restart cannot inherit stale multiprocess metric files;
- the corrected image is deployed and its identity is verified;
- public health and application functionality remain healthy; and
- the ServiceMonitor is restored deliberately and observed through a defined stability window.

Until that closeout is recorded, the correct application state is healthy, the correct telemetry
state is intentionally paused, and the incident status is **Mitigated**.

## Evidence gaps and unknowns

- The historical evidence begins at 23:20 UTC. The first unmatched request that began the buildup
  and the exact impact start are unknown.
- Raw historical request paths and exact source addresses are unavailable or intentionally excluded
  from public evidence. The traffic source cannot be attributed.
- Exact customer request failures and any in-memory work losses were not measured. The latter is an
  unquantified risk, not proven customer data loss.
- Exact PagerDuty trigger and acknowledgement timestamps are unavailable in the sanitized evidence.
- Direct pre-mitigation `kubectl top` and cgroup capture failed after the old container was
  unavailable.
- `container_oom_events` remained zero; Kubernetes termination state overrides that unhelpful
  signal for incident classification.
- `scrape_body_bytes` remained zero; it is not evidence of an empty response. Sanitized application
  access logs provide the approximately 7.79 MB response-size evidence.
- Thirty-second memory samples cannot capture every instantaneous allocation, including the precise
  allocation that crossed the limit.
- No raw URLs, query strings, source addresses, node keys, request identifiers, credentials,
  tokens, ciphertext, prompts, responses, tool data, screenshots, or raw log archives are included
  in this public record.

## Verification commands and public references

The postmortem pair can be checked locally without accessing any production system:

```bash
python -m json.tool outages/2026-09-02-production-relay-metrics-cardinality-oom.json >/dev/null
python - <<'PY'
import json
import jsonschema

with open("outages/schema.json", encoding="utf-8") as handle:
    schema = json.load(handle)
with open(
    "outages/2026-09-02-production-relay-metrics-cardinality-oom.json",
    encoding="utf-8",
) as handle:
    document = json.load(handle)
jsonschema.validate(document, schema, format_checker=jsonschema.FormatChecker())
PY
pre-commit run --all-files
git diff --check
detect-secrets scan outages/2026-09-02-production-relay-metrics-cardinality-oom.md outages/2026-09-02-production-relay-metrics-cardinality-oom.json
```

Durable public references:

- deployed build-label change: [PR #1735](https://github.com/futuroptimist/token.place/pull/1735)
  and commit [`e46277d`](https://github.com/futuroptimist/token.place/commit/e46277daaeb76beeb9f2a2e9e265181287239b22);
- unrelated later Valkey slice: [PR #1726](https://github.com/futuroptimist/token.place/pull/1726); and
- separate shared-state/HA resilience work:
  [issue #1569](https://github.com/futuroptimist/token.place/issues/1569).
