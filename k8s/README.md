# Kubernetes manifests for token.place relay

The production/staging path for token.place on Sugarkube is GHCR-first:
GitHub Actions publishes the canonical relay image and Helm chart, and Sugarkube
deploys immutable GHCR tags. Start with
[`docs/ops/sugarkube-release.md`](../docs/ops/sugarkube-release.md) for the
operator workflow.

## Canonical Sugarkube deploy path

Canonical artifacts:

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Relay image workflow: `.github/workflows/ci-image.yml`
- Chart source: `charts/tokenplace`
- Chart OCI reference: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Chart workflow: `.github/workflows/ci-helm.yml`

Staging deploy flow:

1. Find a successful `ci-image.yml` run on `main`.
2. Copy the immutable image tag from the workflow summary or GHCR package page.
   Use a tag like `main-REPLACE_SHORTSHA`, not `main-latest`, for validation.
3. Confirm or publish the chart with `ci-helm.yml`.
4. From a Sugarkube checkout, deploy with the current app-specific recipe:

   ```bash
   just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
   ```

5. Once Sugarkube P5 lands, use the generic recipe:

   ```bash
   just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
   ```

## Legacy raw manifests

The YAML files in this directory are low-level examples for local k3s
experiments. They are not the canonical Sugarkube release surface and should not
be used for staging or production rollouts.

If you intentionally test these manifests locally, edit the `image:` fields to a
published GHCR tag first, for example:

```yaml
image: ghcr.io/futuroptimist/tokenplace-relay:main-REPLACE_SHORTSHA
```

Then apply them to a scratch namespace:

```bash
kubectl create namespace tokenplace-local
kubectl -n tokenplace-local apply -f k8s/
```

## Raspberry Pi pod manifest

For a local Raspberry Pi k3s experiment, `relay-raspi-pod.yaml` runs a single
ARM64 pod. Prefer the published multi-architecture GHCR image tag above instead
of building on the Pi.

```bash
kubectl -n tokenplace-local apply -f k8s/relay-raspi-pod.yaml
```

The deployment manifest includes resource requests/limits and basic health
probes. Adjust these values according to your cluster capacity.
