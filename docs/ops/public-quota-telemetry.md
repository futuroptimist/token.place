# Public quota pressure telemetry

The relay exposes `tokenplace_public_http_quota_outcomes_total`, a counter for public HTTP quota
decisions. It is deliberately separate from `tokenplace_relay_request_outcomes_total`: the latter
continues to describe terminal inference-request transitions, while this counter includes accepted
and rejected HTTP requests whether or not they submit inference work.

## Finite contract

The counter has four labels with closed vocabularies:

| Label | Allowed values |
| --- | --- |
| `route_class` | `root`, `public_metadata`, `public_version`, `api_v1`, `api_v2`, `operational`, `static`, `control_plane`, `other_known`, `unmatched` |
| `method` | `GET`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`, `HEAD`, `other` |
| `outcome` | `accepted`, `exempt`, `rejected` |
| `reason` | `none`, `hourly_limit`, `daily_limit`, `other_limit`, `other_rejection` |

`route_class` is derived from the application-owned Flask endpoint and route rule. Exact root,
metadata, and version endpoints have stable classes; a request with no matched rule becomes
`unmatched`. The request path, query string, and route parameters are never used as labels.
Unrecognized methods become `other`. Flask-Limiter's trusted limit granularity selects
`hourly_limit` or `daily_limit`; all other limiter windows become `other_limit`, and a 429 not
identified as a limiter breach becomes `other_rejection`. `reason="none"` is used for accepted and
exempt decisions.

The Cartesian upper bound is 10 route classes × 8 methods × 3 outcomes × 5 reasons = **1,200
logical label combinations per target**. Each logical counter combination can produce both a
`_total` sample and a companion `_created` sample, so the conservative Prometheus exposition bound
is **2,400 series**; invalid combinations are not pre-created and
therefore the observed count is substantially smaller. Unique paths, client/source addresses, forwarded headers, limiter
keys, request IDs, tokens, credentials, and exception text cannot add label values. None of those
values, nor raw Prompts or encrypted model payloads, may appear in metric names, help text, or
labels.

The supported pressure signal is the rate or increase of aggregate decisions and rejections. A
remaining-budget gauge is intentionally not exposed: Flask-Limiter budgets are client-keyed, so
publishing per-key remaining state would either create identity-correlated series or require an
ambiguous cross-client aggregation. Rejection ratios and reason-specific increases provide a safe,
actionable signal without exporting client-level state.

## Aggregation and lifecycle

The canonical relay deployment uses one Gunicorn worker (`RELAY_WORKERS=1`) because relay queues
and registrations are process-local. The counter is registered in the relay's dedicated bounded
Prometheus registry and served by the relay-owned `/metrics` endpoint; the default
prometheus-flask-exporter request families remain disabled. Counter values accumulate across
threads in that worker and reset whenever the worker/container restarts. Prometheus queries must
use `sum`, `rate`, or `increase` and tolerate counter resets. Each authenticated scrape is a
point-in-time serialization. The relay-owned endpoint whose trusted Flask endpoint name is
`metrics` is excluded from this counter, so repeated scrapes do not mutate it.

`PROMETHEUS_MULTIPROC_DIR` may be present in the canonical image for the Prometheus client, but the
supported runtime remains exactly one worker. Do not sum both the dedicated registry and a second
default/multiprocess collector. A future multi-worker relay must first implement the shared-state
architecture and then define worker-file cleanup and aggregation as part of that migration.

## Downstream consumption

For [Sugarkube #2405](https://github.com/futuroptimist/sugarkube/issues/2405), dashboards and alerts
should aggregate across instances and use reason-specific rates, for example:

```promql
sum by (route_class, reason) (
  rate(tokenplace_public_http_quota_outcomes_total{outcome="rejected"}[5m])
)
```

Root and metadata panels should select `route_class=~"root|public_metadata"`. An early-pressure
alert should compare the rejected rate with accepted/exempt traffic over a reviewed window, rather
than assuming a client-specific remaining budget. Consumers must preserve the finite values above
and must not join in ingress path, client address, forwarded-header, request-ID, or credential
labels.

For [Sugarkube #2782](https://github.com/futuroptimist/sugarkube/issues/2782), retained incident
evidence may store only time-windowed aggregates grouped by the four labels in this contract,
along with scrape target/build metadata already approved elsewhere. It must not retain individual
requests, limiter keys, identities, headers, raw URLs, query strings, or exception bodies. Record
the query window and account for resets when reporting an `increase()`.
