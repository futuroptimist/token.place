# Emergency application-metrics mode

The emergency application-metrics mode is **defense in depth** for a telemetry incident. Permanently
bounded normal-mode instrumentation remains the cardinality fix. Do not use this mode to accept or
postpone an unbounded collector.

## Contract and finite bound

`TOKENPLACE_METRICS_MODE` is read once during process startup. It accepts exactly the case-sensitive
values `normal` and `degraded`; omission selects `normal`. Empty, whitespace-padded, differently
cased, and unknown values stop startup. There is no automatic memory trigger, runtime endpoint,
exception fallback, or in-process mode mutation. A change therefore requires a reviewed
configuration update and controlled restart or rollout.

Normal mode retains the dedicated bounded registry described in
[the v0.1.2 observability contract](releases/v0.1.2.md#implemented-relay-metrics-slice-source-level-not-live-evidence). Its families are:

- `tokenplace_public_http_quota_outcomes_total`;
- `tokenplace_relay_requests_total`;
- `tokenplace_http_requests_total` and `tokenplace_http_request_duration_seconds`;
- `tokenplace_relay_queue_depth` and `tokenplace_relay_oldest_queued_request_age_seconds`;
- `tokenplace_compute_nodes_registered`, `tokenplace_compute_nodes_healthy`,
  `tokenplace_compute_node_lease_age_seconds`, and `tokenplace_compute_node_evictions_total`;
- `tokenplace_relay_in_flight_requests` and `tokenplace_relay_oldest_in_flight_age_seconds`;
- `tokenplace_relay_request_outcomes_total`;
- `tokenplace_relay_compute_control_requests_total` and
  `tokenplace_relay_compute_control_lease_renewals_total`;
- `tokenplace_build_info`; and
- `tokenplace_instrumentation_up`.

Degraded mode constructs a separate registry containing exactly these three gauge series:

| Metric | Labels and vocabulary | Series ceiling | Meaning |
| --- | --- | ---: | --- |
| `tokenplace_build_info` | `version=<one startup release value>`, `revision=<one startup build value>` | 1 | Bounded release identity. |
| `tokenplace_instrumentation_up` | none | 1 | Minimal instrumentation initialization health (`1` after success). |
| `tokenplace_metrics_degraded` | none | 1 | Explicit intentional degraded state (`1`). |

The complete degraded ceiling is **3 series per relay process**. There are no path, route, method,
status, identity, address, forwarding, limiter, request, exception, process, worker, credential, or
token labels. Default Flask collectors remain disabled. Suppressed relay collectors are not
constructed or registered; the public-quota metrics callback is not installed; request accounting
and scrape-time runtime-gauge traversal are bypassed. Consequently requests and scrapes cannot
allocate metric label children or mutate the three values.

## Activation

Use this only when bounded normal instrumentation is suspected of threatening relay availability.
Before activation, preserve privacy-safe aggregate evidence, confirm `/livez` and `/healthz`, record
the immutable image/build identity, confirm the metrics bearer Secret is available, and obtain the
normal scrape baseline. Do not place credentials or request data in evidence.

For the Helm deployment, set:

```yaml
metrics:
  enabled: true
  mode: degraded
```

Keep the existing `metrics.auth.existingSecret` and ServiceMonitor settings unchanged. Render and
review the chart, then perform the normal controlled restart/rollout. This repository change does
not alter a live ServiceMonitor or deploy either mode.

## Verification and monitoring consequences

Confirm the authenticated endpoint still returns HTTP 200 and only the three families above. Then
use these checks (add the deployment's normal target selectors):

```promql
up == 1
tokenplace_metrics_degraded == 1
tokenplace_instrumentation_up == 1
tokenplace_build_info == 1
count({__name__=~"tokenplace_.+"}) == 3
count({__name__=~"tokenplace_(http_requests|relay_queue_depth|public_http_quota_outcomes).+"}) == 0
```

Also compare two authenticated payloads around repeated scrapes and representative unique requests;
they must be byte-for-byte equal. `/livez` must retain `status: alive`, while `/healthz` retains its
existing readiness, draining, and dependency semantics.

Degraded mode deliberately makes request rate/latency/status, public-quota pressure, relay outcomes,
queue age/depth, in-flight work, compute-node health/lease/eviction, and compute-control activity
dashboards and alerts unavailable. Scrape availability, build identity, instrumentation health, and
the degraded-state alert remain available. Route operational decisions to health checks, safe logs,
and external probes while the mode is active; do not infer that absent application series are zero.

## Restoration and rollback

1. Correct and qualify the normal-mode telemetry failure first.
2. Set `metrics.mode: normal`, render and review the chart, and perform a controlled restart/rollout.
3. Verify `tokenplace_metrics_degraded` is absent, all normal families have returned, authentication
   is unchanged, raw paths remain absent, and bounded-series checks pass.
4. Soak normal mode while watching scrape duration/size, process memory, restarts, and all restored
   dashboards and alerts. **Do not declare the telemetry incident resolved until normal mode is
   restored and has completed the approved soak.**

If the new process is unhealthy, roll back the configuration and image through the normal deployment
procedure. If degraded mode or its three-series scrape is itself unsafe, pausing the ServiceMonitor
remains the last-resort rollback; retain `/livez` and `/healthz` probing and restore authenticated
application scraping as soon as it is safe.
