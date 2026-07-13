# tokenplace Helm chart

`charts/tokenplace` is the canonical Sugarkube/GHCR chart source for the token.place relay.
Metrics are disabled by default and are exposed only as an opt-in, authenticated in-cluster scrape contract.

## Authenticated Prometheus scraping

Create the bearer-token Secret out of band; do not put token material in values files or Git. Because Prometheus Operator reads the ServiceMonitor Secret from the ServiceMonitor namespace while the relay pod reads its environment Secret from the app namespace, mirror the same Secret name/key into both namespaces:

```bash
kubectl -n tokenplace create secret generic tokenplace-metrics --from-literal=token="$TOKENPLACE_METRICS_TOKEN"
kubectl -n monitoring create secret generic tokenplace-metrics --from-literal=token="$TOKENPLACE_METRICS_TOKEN"
```

Enable the contract explicitly:

```yaml
metrics:
  enabled: true
  path: /metrics
  auth:
    existingSecret: tokenplace-metrics
    secretKey: token
serviceMonitor:
  enabled: true
  namespace: monitoring
  additionalLabels:
    release: kube-prometheus-stack
  targetLabels:
    app: tokenplace
    environment: staging
    release: tokenplace
    cluster: sugarkube-staging
```

The rendered ServiceMonitor selects the canonical token.place Service by its Helm selector labels, scrapes named port `http` at `/metrics`, and uses the Prometheus Operator `authorization.credentials` SecretKeySelector supported by Sugarkube's pinned `kube-prometheus-stack` chart (`58.2.0`, Prometheus Operator `v0.73.1`). It does not create a public `/metrics` ingress.

## Verification commands

```bash
helm template tokenplace ./charts/tokenplace --namespace tokenplace | grep -qv 'kind: ServiceMonitor'
helm template tokenplace ./charts/tokenplace --namespace tokenplace \
  --set metrics.enabled=true \
  --set metrics.auth.existingSecret=tokenplace-metrics \
  --set serviceMonitor.enabled=true > /tmp/tokenplace-metrics.yaml
kubectl -n monitoring get servicemonitor tokenplace -o jsonpath='{.metadata.labels.release}{"\n"}'
kubectl -n monitoring get secret tokenplace-metrics -o jsonpath='{.data.token}' >/dev/null
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090
curl -fsS 'http://127.0.0.1:9090/api/v1/targets?state=active' | jq '.data.activeTargets[] | select(.labels.app=="tokenplace") | {health, scrapeUrl, labels}'
curl -i https://staging.token.place/metrics
```

A public unauthenticated `/metrics` request should be denied by the relay when staging or production metrics are enabled; if it returns metric text, do not promote the release.
