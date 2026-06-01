# Kubernetes deployment notes for token.place relay

Sugarkube deployments use the canonical GHCR image and OCI Helm chart, not a
local Docker build or the legacy manifests in this directory.

## Canonical Sugarkube path

1. Find a successful **Build and publish GHCR relay image** run
   (`.github/workflows/ci-image.yml`).
2. Copy the immutable Sugarkube tag from the workflow summary, for example
   `main-REPLACE_SHORTSHA`.
3. Confirm the **Publish Helm chart** workflow (`.github/workflows/ci-helm.yml`)
   has validated and published the chart version you intend to deploy.
4. Deploy from a Sugarkube checkout with the current app-specific command:

   ```bash
   just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
   ```

5. After Sugarkube P5 lands, use the generic app command:

   ```bash
   just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
   ```

Canonical artifacts:

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart source: `charts/tokenplace`
- OCI chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`

See [docs/ops/sugarkube-release.md](../docs/ops/sugarkube-release.md) for the
full release contract and chart/image tag rules.

## Local-development appendix: legacy manifests

The raw manifests in this directory are retained for low-level Kubernetes
experiments only. They reference a local `tokenplace-relay:latest` image and do
not represent the staging or production Sugarkube deploy path.

If you intentionally test the raw manifests in a throwaway cluster, first build
or import a local development image, then apply the manifests manually:

```bash
docker build -t tokenplace-relay:latest -f Dockerfile .
kubectl apply -f k8s/relay-deployment.yaml
kubectl apply -f k8s/relay-service.yaml
```

Edit the `image:` field in the manifest for your test registry if needed. Do not
promote this flow into Sugarkube runbooks.

## Raspberry Pi pod manifest

For a Raspberry Pi k3s scratch cluster, `relay-raspi-pod.yaml` can run a single
ARM64 pod after you have supplied a compatible local image. This remains a
local-development escape hatch; Sugarkube operators should deploy the OCI chart
with the GHCR tag copied from `ci-image.yml`.
