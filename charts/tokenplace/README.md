# token.place Helm chart

Canonical Sugarkube/GHCR chart for the token.place relay, published as
`oci://ghcr.io/futuroptimist/charts/tokenplace`.

## Metrics and ServiceMonitor

Metrics scraping is opt-in and authenticated. Defaults render no metrics token
environment variable and no `ServiceMonitor`.

Create the token secret in the same namespace as the token.place release and the
`ServiceMonitor`:

```bash
kubectl -n tokenplace create secret generic tokenplace-metrics \
  --from-literal=token='REPLACE_WITH_RANDOM_TOKEN'
```

Enable the internal scrape contract without publishing `/metrics` through
Ingress:

```yaml
metrics:
  enabled: true
  path: /metrics
  auth:
    existingSecret: tokenplace-metrics
    secretKey: token
serviceMonitor:
  enabled: true
  additionalLabels:
    release: kube-prometheus-stack
  relabelings:
    app: tokenplace
    environment: staging
    release: tokenplace
    cluster: sugarkube
```

The chart injects `TOKENPLACE_METRICS_TOKEN` from the existing Secret into the
relay pod and configures Prometheus Operator `authorization.credentials` so
Prometheus sends the same bearer token when scraping the named `http` Service
port. Do not put plaintext token values in values files or Git.

Verification commands:

```bash
helm template tokenplace charts/tokenplace --namespace tokenplace \
  --set metrics.enabled=true \
  --set metrics.auth.existingSecret=tokenplace-metrics \
  --set serviceMonitor.enabled=true > /tmp/tokenplace-metrics.yaml
kubectl -n tokenplace get servicemonitor tokenplace -o jsonpath='{.metadata.labels.release}{"\n"}'
kubectl -n tokenplace get endpoints tokenplace
kubectl -n monitoring exec statefulset/prometheus-kube-prometheus-stack-prometheus -c prometheus -- \
  wget -qO- --header="Authorization: Bearer $(kubectl -n tokenplace get secret tokenplace-metrics -o jsonpath='{.data.token}' | base64 -d)" \
  http://tokenplace.tokenplace.svc/metrics | head
curl -fsS -o /dev/null -w '%{http_code}\n' https://staging.token.place/metrics
kubectl -n tokenplace get ingress tokenplace -o yaml | grep -n '/metrics' || true
```

Expected results: the release label is `kube-prometheus-stack`, endpoints point
at the token.place Service, the internal scrape returns Prometheus text, the
public request is denied by the application when staging/production metrics are
enabled, and the Ingress contains no dedicated `/metrics` path.

The chart intentionally preserves one replica, one relay worker, `Recreate`
rollouts, and in-memory relay state. Observability must not change that relay
architecture.
