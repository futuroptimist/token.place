# Kubernetes manifests for token.place relay

The canonical staging and production path is the Sugarkube GHCR-first release flow, not local Docker
image builds and not the legacy in-repo Helm bundle.

## Canonical Sugarkube deployment

Use the published relay image and OCI Helm chart:

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Chart source: [`../charts/tokenplace`](../charts/tokenplace)

From a Sugarkube checkout:

```bash
just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
```

Once Sugarkube P5 lands, use the generic app deploy command:

```bash
just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
```

Replace `main-REPLACE_SHORTSHA` with the immutable tag printed by a successful token.place
`ci-image.yml` run, such as `main-deadbee`. Confirm the chart version with `ci-helm.yml` or GHCR
before deploying.

## Legacy raw manifests

The YAML files in this directory are retained for local Kubernetes experiments and historical
Raspberry Pi notes. They are not the Sugarkube contract and should not be used for staging or
production.

If you need a throwaway manifest test, pin the Deployment or Pod to a published GHCR tag first:

```bash
kubectl -n tokenplace set image deployment/tokenplace-relay \
  relay=ghcr.io/futuroptimist/tokenplace-relay:main-REPLACE_SHORTSHA
```

Then apply only the resources you understand in a disposable namespace:

```bash
kubectl create namespace tokenplace
kubectl -n tokenplace apply -f k8s/relay-deployment.yaml -f k8s/relay-service.yaml
```

## Local-development appendix

Local Docker builds are for developer-only experiments. Do not use them as a prerequisite for
staging or production deploys.

```bash
docker build -t tokenplace-relay:dev -f Dockerfile .
```
