# Emergency bounded application-metrics mode

`TOKENPLACE_METRICS_MODE` is a startup-only defense-in-depth control for a telemetry incident. It
does not replace the bounded normal registry or fix cardinality defects. The only accepted values
are the exact, case-sensitive strings `normal` and `degraded`; omission selects `normal`, while an
empty, malformed, unknown, or case-variant value stops application startup. Changing mode requires
an explicit configuration change and a controlled restart or rollout. There is no automatic,
memory-triggered, error-triggered, or runtime transition.

The Helm value is `metrics.mode` and defaults to `normal`. Keep `metrics.enabled`, the existing
metrics bearer-token Secret, and the ServiceMonitor unchanged when selecting degraded mode.

## Finite registry contracts

Normal mode preserves all application collectors, their current bounded labels, request hooks,
runtime-gauge collection, public-quota telemetry, relay lifecycle/queue/compute signals, and build
identity. Its metric families are:

- `tokenplace_relay_requests_total`;
- `tokenplace_http_requests_total` and `tokenplace_http_request_duration_seconds`;
- `tokenplace_relay_queue_depth` and `tokenplace_relay_oldest_queued_request_age_seconds`;
- `tokenplace_compute_nodes_registered`, `tokenplace_compute_nodes_healthy`,
  `tokenplace_compute_node_lease_age_seconds`, and `tokenplace_compute_node_evictions_total`;
- `tokenplace_relay_in_flight_requests`, `tokenplace_relay_oldest_in_flight_age_seconds`, and
  `tokenplace_relay_request_outcomes_total`;
- `tokenplace_relay_compute_control_requests_total` and
  `tokenplace_relay_compute_control_lease_renewals_total`;
- `tokenplace_public_http_quota_outcomes_total`;
- `tokenplace_build_info`, `tokenplace_instrumentation_up`, and
  `tokenplace_metrics_degraded` (fixed at `0`).

Degraded mode constructs a separate `CollectorRegistry` containing exactly three samples:

| Metric | Labels / values | Maximum series |
| --- | --- | ---: |
| `tokenplace_build_info` | one startup-selected `version` and public `revision`; value `1` | 1 |
| `tokenplace_instrumentation_up` | no labels; value `1` after successful initialization | 1 |
| `tokenplace_metrics_degraded` | no labels; fixed value `1` | 1 |

Initialization publishes `tokenplace_metrics_degraded = 1` independently before checking any other collector failure. If the degraded-state gauge cannot be constructed or initialized, the relay fails startup with fixed, privacy-safe diagnostics rather than serving an ambiguous metrics response.

The exact degraded ceiling is therefore **3 time series per relay process**. Build labels come only
from bounded startup release metadata, not requests. No request path, query, route, identity,
address, forwarding header, limiter key, request ID, exception, process/worker ID, credential, or
token is a degraded label. Default Flask collectors, public-quota metrics, request counters and
histograms, queue/compute/lifecycle collectors, initialization of their label children, request
metric updates, and scrape-time runtime callbacks are not registered or run. A scrape only
serializes the three fixed samples and does not mutate them.

## Controlled activation

Before activation, confirm that the suspected failure is application instrumentation, preserve
the current image and configuration for rollback, confirm the metrics Secret remains available,
and record which normal-mode alerts will be lost. Render and review the repository chart change:

```sh
helm template tokenplace charts/tokenplace --namespace tokenplace \
  --set metrics.enabled=true \
  --set metrics.mode=degraded \
  --set metrics.auth.existingSecret=tokenplace-metrics
```

Apply the equivalent private environment/value-file change through the normal deployment system
and perform a controlled rollout. Do not add incident-specific coordinates to this repository and
do not mutate a live ServiceMonitor as part of activation.

Verify authenticated scrape availability and the finite contract:

```sh
curl -fsS -H "Authorization: Bearer ${TOKENPLACE_METRICS_TOKEN}" \
  "${RELAY_BASE_URL}/metrics" > /tmp/tokenplace-degraded.prom
promtool check metrics < /tmp/tokenplace-degraded.prom
grep -E '^tokenplace_(build_info|instrumentation_up|metrics_degraded)' \
  /tmp/tokenplace-degraded.prom
```

PromQL checks are:

```promql
up{job=~".*tokenplace.*"} == 1
tokenplace_metrics_degraded == 1
tokenplace_instrumentation_up == 1
tokenplace_build_info == 1
count({__name__=~"tokenplace_.+"}) == 3
absent(tokenplace_http_requests_total)
absent(tokenplace_public_http_quota_outcomes_total)
absent(tokenplace_relay_queue_depth)
```

Also verify `/livez` and `/healthz` independently. Authentication remains identical: missing or
incorrect bearer credentials still return HTTP 401, and the configured credential never appears
in exposition or diagnostics.

## Monitoring consequences and restoration

In degraded mode, dashboards and alerts based on HTTP rate/duration/outcome, public-quota pressure,
relay queue and in-flight work, compute registration/health/lease/eviction, and compute-control
activity are unavailable. Only scrape health, build identity, instrumentation health, and explicit
degraded state remain. Inference, compute registration and polling, control-plane traffic, public
information exemptions, quotas, `/livez`, and `/healthz` continue to operate, but application
metrics cannot be used to assess them.

After repairing and qualifying normal instrumentation, set `metrics.mode: normal`, render and
review it, and perform another controlled restart/rollout. Confirm `tokenplace_metrics_degraded ==
0`, the suppressed families have returned, normal dashboards and alerts have data, and registry
cardinality and memory remain within their reviewed budgets. **Do not declare the telemetry
incident resolved until normal mode has been restored and soaked.**

If the degraded endpoint itself is unsafe, pausing only this application's ServiceMonitor remains
the last-resort rollback. Preserve `/livez` and `/healthz` probing, follow the incident runbook, and
restore authenticated scraping as soon as it is safe.
