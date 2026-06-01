# Kubernetes manifests for the token.place relay

These manifests are legacy, local-development Kubernetes examples for running `relay.py` as a
Deployment/Pod and exposing it via a Service. They are not the canonical Sugarkube staging or
production release path.

## Canonical Sugarkube release path

Sugarkube deployments should use the GHCR-published relay image and OCI Helm chart:

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Chart source in this repo: `charts/tokenplace`

Use the release runbook instead of building or importing a local image first:

1. Find a successful `ci-image.yml` run for the candidate commit.
2. Copy the immutable tag from the workflow summary or GHCR package page, for example
   `main-REPLACE_SHORTSHA`.
3. Confirm that `ci-helm.yml` has validated and published the chart version you intend to deploy.
4. From a Sugarkube checkout, deploy with the current app-specific recipe:

   ```bash
   just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
   ```

5. Once Sugarkube generic app recipes land, use:

   ```bash
   just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
   ```

See [`docs/ops/sugarkube-release.md`](../docs/ops/sugarkube-release.md) for the full GHCR-first
workflow and chart/image publishing contract.

## Local-development manifest appendix

For local clusters only, you can build the canonical root Dockerfile and apply the example manifests:

```bash
docker build -t tokenplace-relay:local -f Dockerfile .
kubectl apply -f k8s/
```

Edit manifest `image:` fields or import `tokenplace-relay:local` into your local k3s/containerd
runtime as needed. Do not use these local manifest steps for Sugarkube staging/prod releases.

## Raspberry Pi pod manifest

For a Raspberry Pi k3s development cluster, `relay-raspi-pod.yaml` runs a single ARM64 pod and sets
`TOKEN_PLACE_ENV=production` so the Service and observability stacks can reach it:

```bash
kubectl apply -f k8s/relay-raspi-pod.yaml
```

The deployment manifest includes resource requests/limits and basic health probes. Adjust these
values according to your cluster capacity.
