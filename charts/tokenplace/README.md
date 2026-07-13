# tokenplace Helm chart

This is the canonical Sugarkube chart published by `.github/workflows/ci-helm.yml`
as `oci://ghcr.io/futuroptimist/charts/tokenplace`.

## Authenticated metrics scrape

Metrics scraping is opt-in and internal-only by default. The chart does not create
or route a public `/metrics` Ingress path. When metrics are enabled, the relay
receives `TOKENPLACE_METRICS_TOKEN` from an existing Kubernetes Secret and denies
unauthenticated `/metrics` requests.

Create the token Secret in the token.place namespace:

```sh
kubectl -n tokenplace create secret generic tokenplace-metrics \
  --from-literal=token='<random bearer token>'
```

Enable the scrape contract for the canonical Service and the Sugarkube
kube-prometheus-stack discovery label:

```sh
helm upgrade --install tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace \
  --namespace tokenplace --create-namespace \
  --set metrics.enabled=true \
  --set metrics.auth.existingSecret=tokenplace-metrics \
  --set serviceMonitor.enabled=true \
  --set serviceMonitor.additionalLabels.release=kube-prometheus-stack \
  --set serviceMonitor.relabelings.environment=staging \
  --set serviceMonitor.relabelings.release=v0.1.2 \
  --set serviceMonitor.relabelings.cluster=sugarkube-staging
```

The ServiceMonitor selects the canonical `tokenplace` Service by its Helm selector
labels and scrapes the named `http` port at `metrics.path` (`/metrics` by
default). The bearer token is referenced with the Prometheus Operator
`bearerTokenSecret` endpoint field so the token remains in a Kubernetes Secret;
no plaintext token value belongs in chart values, rendered manifests, logs, or
Git.

### Verification commands

Internal scrape success from the token.place namespace:

```sh
TOKEN="$(kubectl -n tokenplace get secret tokenplace-metrics -o jsonpath='{.data.token}' | base64 -d)"
kubectl -n tokenplace run tokenplace-metrics-curl --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 -- \
  curl -fsS -H "Authorization: Bearer ${TOKEN}" \
  http://tokenplace.tokenplace.svc.cluster.local/metrics >/tmp/tokenplace.metrics
```

Public denial when staging or production metrics are enabled:

```sh
curl -i https://staging.token.place/metrics | sed -n '1,5p'
```

The response must not be a successful Prometheus metrics response. A `401` from
the relay or a public edge denial is acceptable; a public unauthenticated `200`
with metrics is not acceptable.

Release identity and target labels in Prometheus:

```sh
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
curl -fsS 'http://127.0.0.1:9090/api/v1/targets?state=active' \
  | jq '.data.activeTargets[] | select(.labels.job | test("tokenplace")) | {scrapeUrl, labels, lastScrape, health}'
curl -fsS 'http://127.0.0.1:9090/api/v1/query?query=up{app="tokenplace"}' | jq .
```

The discovered target should include the configured bounded labels: `app`,
`environment`, `release`, and `cluster`.

## Relay constraints

Do not change observability by changing the relay architecture. The chart keeps
one replica, one Gunicorn worker, `Recreate` rollout strategy, and the in-memory
relay state model. Registrations, queues, in-flight requests, and replies are
expected to be lost on pod restart until a future shared-state architecture is
implemented.
