# Emergency bounded application metrics

`TOKENPLACE_METRICS_MODE` is a startup-only defense-in-depth control for a telemetry incident. It
accepts exactly `normal` (the default when unset) or `degraded`. Empty, differently cased, padded,
or unknown values stop relay startup. This mode is not a substitute for keeping normal metrics
permanently bounded and it does not switch automatically in response to memory or scrape errors.

The Helm value is `metrics.mode`. Changing it requires a reviewed configuration change and a
controlled restart/rollout. It does not alter `metrics.enabled`, the `ServiceMonitor`, or the bearer
token Secret. Never use degraded mode to work around an application or relay-state failure.

## Finite degraded registry

Degraded mode constructs a separate, process-local `CollectorRegistry`. Only these three gauge
series are registered:

| Metric | Labels | Values | Maximum series |
| --- | --- | --- | ---: |
| `tokenplace_build_info` | `version`, `revision` from bounded release metadata | exactly one startup identity pair, value `1` | 1 |
| `tokenplace_instrumentation_up` | none | `0` during initialization, then `1` | 1 |
| `tokenplace_metrics_degraded` | none | `1` in degraded mode (`0` in normal mode) | 1 |

The exact degraded ceiling is **three series per relay process**. Scrapes do not update gauges.
Request hooks skip HTTP counters and histograms, lifecycle and compute-control update helpers return
without creating labels, runtime queue/node callbacks do not run during scrapes, and the public-quota
collector and callback are not installed. Default `prometheus_flask_exporter` collectors remain
disabled. Thus paths, route templates, query strings, identities, addresses, forwarding headers,
limiter keys, request IDs, exception text, process/worker IDs, credentials, and tokens cannot become
degraded labels. Relay traffic, quotas, compute polling and registration, control-plane behavior,
`/livez`, and `/healthz` continue operating; only their application metric updates are suppressed.

Normal mode retains the complete bounded registry documented in
[the v0.1.2 observability contract](../releases/v0.1.2.md#metric-definitions), including HTTP,
latency, queue, in-flight, compute-node lifecycle/control, outcome, build identity,
instrumentation-health, compatibility, and bounded public-quota families. It additionally exports
`tokenplace_metrics_degraded 0`.

## Activation and verification

Preconditions: confirm an application-instrumentation memory/cardinality incident, preserve the
current metrics bearer Secret, record the image/build identity, and obtain incident approval. Set
`metrics.mode: degraded` in reviewed values and perform a controlled rollout. Do not mutate a live
process or add incident-specific cluster coordinates.

After rollout, verify authenticated scrape availability and the exact contract:

```sh
curl -fsS -H "Authorization: Bearer $TOKENPLACE_METRICS_TOKEN" \
  "$RELAY_INTERNAL_URL/metrics" > /tmp/tokenplace-metrics
grep '^tokenplace_metrics_degraded 1' /tmp/tokenplace-metrics
grep '^tokenplace_instrumentation_up 1' /tmp/tokenplace-metrics
grep '^tokenplace_build_info{' /tmp/tokenplace-metrics
grep -Ev '^(#|$|tokenplace_(build_info\{|instrumentation_up |metrics_degraded ))' \
  /tmp/tokenplace-metrics && { echo 'unexpected metric family' >&2; exit 1; } || true
curl -fsS "$RELAY_INTERNAL_URL/livez"
curl -fsS "$RELAY_INTERNAL_URL/healthz"
```

Prometheus checks are:

```promql
up{job="tokenplace"} == 1
tokenplace_metrics_degraded == 1
tokenplace_instrumentation_up == 1
tokenplace_build_info == 1
count({__name__=~"tokenplace_http_.*|tokenplace_relay_.*|tokenplace_compute_.*|tokenplace_public_.*"}) == 0
```

The last query intentionally excludes the retained build and mode families. Adapt only the external
target selector (`job`, namespace, or release) to the environment; do not add production coordinates
to this repository.

## Monitoring consequences and restoration

Degraded mode removes application HTTP throughput/latency, public quota pressure, queue depth/age,
in-flight work/age, compute-node health/lease/eviction/control, and terminal outcome signals.
Dashboards and alerts based on those families will show no data and must be treated as unavailable,
not healthy. Kubernetes/platform pod CPU, memory, restart, blackbox, and target-`up` monitoring are
external to this registry and should remain the operational safety net.

Once the telemetry hazard is fixed and a bounded normal registry has been reviewed, set
`metrics.mode: normal` (or remove the setting) and perform another controlled rollout. Verify the
three retained signals, confirm `tokenplace_metrics_degraded == 0`, confirm normal families have
returned, and soak normal scraping while watching pod memory, restarts, scrape duration, and series
growth. **Do not declare the telemetry incident resolved until normal mode is restored and has
completed the agreed soak.**

If degraded mode itself is unsafe, roll back the image/configuration. Pausing the `ServiceMonitor`
and losing application scrapes remains the last-resort rollback; it is an operator action outside
this repository and must retain external liveness and resource monitoring.
