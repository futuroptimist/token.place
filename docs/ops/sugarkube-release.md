# token.place Sugarkube release contract

This is the canonical release path for token.place on Sugarkube. It is GHCR-first: GitHub Actions
publishes the relay image and Helm chart, then Sugarkube deploys by immutable image tag and immutable
OCI chart version. Do not build local Docker images or deploy local chart paths for staging or
production.

## Canonical artifacts

- Relay Dockerfile: [`Dockerfile`](../../Dockerfile)
- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Helm chart source: [`charts/tokenplace`](../../charts/tokenplace)
- Helm OCI chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Chart name: `tokenplace`

The relay is intentionally single-pod for the current in-memory state model: one replica, one
Gunicorn worker, multiple threads, and `Recreate` rollout semantics. The chart also keeps XDG state
under `/tmp` so the relay can run with a read-only root filesystem.

## Image workflow

The canonical image workflow is `.github/workflows/ci-image.yml`.

- Pull requests build the root `Dockerfile` and smoke-test the relay container, but do not publish.
- Pushes to `main` publish:
  - `ghcr.io/futuroptimist/tokenplace-relay:main-<shortsha>`
  - `ghcr.io/futuroptimist/tokenplace-relay:main-latest`
  - `ghcr.io/futuroptimist/tokenplace-relay:sha-<shortsha>`
- Semver Git tag pushes such as `v0.1.0` publish:
  - `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`
  - `ghcr.io/futuroptimist/tokenplace-relay:sha-<shortsha>`
- Manual `workflow_dispatch` runs validate a selected ref only; they do not publish images.

For staging, deploy the immutable `main-<shortsha>` tag from a successful `main` run. Treat
`main-latest` as a lint/render convenience only.

## Chart workflow

The canonical chart workflow is `.github/workflows/ci-helm.yml`.

- Pull requests lint, render, and package `charts/tokenplace`, but do not publish.
- Pushes to `main` and manual runs publish `oci://ghcr.io/futuroptimist/charts/tokenplace` when the
  chart version does not already exist.
- The publish job refuses to overwrite an existing chart version. Bump `charts/tokenplace/Chart.yaml`
  before publishing a changed chart.

Check whether a chart version already exists before depending on it:

```bash
CHART_VERSION=0.1.0
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version "$CHART_VERSION"
```

If that command succeeds for a version you expected to publish, stop and choose a new chart version
instead of overwriting the immutable OCI artifact.

## Staging deployment from Sugarkube

Run these commands from a Sugarkube checkout, not from token.place.

1. Find a successful **Build and publish GHCR relay image** (`ci-image.yml`) run on `main`.
2. Copy the immutable tag from the workflow summary or GHCR package page.
3. Confirm the chart version from **Publish Helm chart** (`ci-helm.yml`) exists or publish it from
   GitHub Actions.
4. Deploy staging with the current token.place-specific Sugarkube recipe:

```bash
just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
```

After Sugarkube P5 lands, use the generic app recipe instead:

```bash
just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
```

Replace `main-REPLACE_SHORTSHA` with the immutable tag printed by `ci-image.yml`, for example
`main-deadbee`.

## Local-development appendix

Local image builds are for developer-only experiments on a workstation or throwaway cluster. They
are not part of staging or production deploys.

```bash
docker build -t tokenplace-relay:dev -f Dockerfile .
```

If you test the chart locally, still keep the canonical image repository and override only the tag
or digest you are validating:

```bash
helm template tokenplace charts/tokenplace \
  --namespace tokenplace \
  --set ingress.enabled=true \
  --set ingress.host=staging.token.place \
  --set image.tag=main-REPLACE_SHORTSHA
```
