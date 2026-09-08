# Emergency application-metrics mode

The relay's bounded normal metrics remain the primary cardinality control. The emergency mode is
**defense in depth** for a telemetry incident: it keeps an authenticated scrape target available
while preventing normal application collectors from being registered or updated.

## Contract and finite cardinality

`TOKENPLACE_METRICS_MODE` is read once at process startup. It accepts exactly `normal` (the default
only when the variable is absent) or `degraded`. Empty, malformed, whitespace-padded, and
case-variant values stop startup. There is no automatic memory trigger, runtime endpoint, exception
fallback, or in-process mode switch.

Normal mode exports the complete bounded registry: HTTP request counts and duration, relay request
outcomes, queue and in-flight state, compute-node lifecycle and control traffic, bounded public-quota
outcomes, build identity, and instrumentation health. Its existing fixed label vocabularies and
release-safety expectations are unchanged.

Degraded mode registers exactly these three gauge series:

| Metric | Labels | Values / maximum series |
| --- | --- | --- |
| `tokenplace_build_info` | `version`, `revision` from bounded release metadata at startup | value `1`; 1 series |
| `tokenplace_instrumentation_up` | none | `0` or `1`; 1 series |
| `tokenplace_metrics_degraded` | none | `1`; 1 series |

The exact ceiling is **three series per relay process**. No request-derived labels exist. Default
Flask collectors, request hooks, public-quota metric callbacks, runtime gauge collection, and all
normal relay collectors are absent or stateless no-ops, so requests and scrapes cannot allocate
metric children. Liveness, readiness, inference, compute polling/registration, control-plane rate
limits, public-information exemptions, and the `/metrics` bearer-token policy remain operational.

## Controlled activation

1. Confirm bounded normal-mode exposition is implicated and preserve privacy-safe aggregate evidence.
2. Keep the current `/metrics` token Secret and ServiceMonitor. Set Helm `metrics.mode: degraded`.
3. Review the rendered Deployment for `TOKENPLACE_METRICS_MODE=degraded`, then perform a controlled
   restart/rollout. Changing configuration without restarting does nothing.
4. Verify scrape and the exact registry (substitute the deployment's existing authenticated access):

   ```promql
   up{job=~".*tokenplace.*"} == 1
   tokenplace_metrics_degraded == 1
   tokenplace_instrumentation_up == 1
   tokenplace_build_info == 1
   ```

   An authenticated `curl` response must contain exactly three non-comment samples. Check absence
   with `absent(tokenplace_http_requests_total)`,
   `absent(tokenplace_public_http_quota_outcomes_total)`, and
   `absent(tokenplace_relay_queue_depth)` (each returns `1`). Verify `/livez` and `/healthz`
   independently and monitor container memory/restarts and blackbox probes.

## Monitoring consequences and restoration

HTTP traffic/error/latency, quota pressure, queue depth/age, in-flight work, request outcomes,
compute-node health/leases/evictions, and compute-control dashboards and alerts are unavailable in
degraded mode. Only target availability, build identity, and instrumentation/degraded state remain.

After correcting and qualifying normal instrumentation, set `metrics.mode: normal`, render and review
the Deployment, and perform another controlled restart/rollout. Confirm
`absent(tokenplace_metrics_degraded) == 1`, normal families have returned, the release-safety gate
passes, and cardinality, payload size, scrape latency, memory, and restarts remain bounded through an
operator-chosen soak. **Do not declare the telemetry incident resolved until normal mode is restored
and has soaked successfully.** Roll back the application/configuration change if restoration is
unsafe. If degraded mode itself is unsafe, pausing only this application's ServiceMonitor remains the
last-resort rollback; follow the incident runbook and leave health probes and unrelated targets intact.
