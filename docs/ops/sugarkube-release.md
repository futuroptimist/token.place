# token.place Sugarkube release workflow

This is the canonical release path for deploying the token.place relay on Sugarkube.
It is GHCR-first: GitHub Actions publishes the relay image and Helm chart, then
Sugarkube deploys those immutable artifacts. Do **not** build local Docker images
for staging or production deploys.

## Canonical artifacts

- Relay image workflow: `.github/workflows/ci-image.yml`
- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Relay Dockerfile: `Dockerfile` at the repository root
- Chart workflow: `.github/workflows/ci-helm.yml`
- Chart source: `charts/tokenplace`
- Chart OCI reference: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Chart name: `tokenplace`

## Image tag contract

`ci-image.yml` validates pull requests and publishes only from trusted repository
pushes:

- Pull requests: build and smoke-test only; no GHCR push.
- `main` pushes publish:
  - `ghcr.io/futuroptimist/tokenplace-relay:main-<shortsha>`
  - `ghcr.io/futuroptimist/tokenplace-relay:main-latest`
  - `ghcr.io/futuroptimist/tokenplace-relay:sha-<shortsha>`
- Semver tag pushes such as `v0.1.0` publish:
  - `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`
  - `ghcr.io/futuroptimist/tokenplace-relay:sha-<shortsha>`
- `workflow_dispatch` builds the selected ref for validation only. It does not
  publish images; push `main` or a semver Git tag when a deployable tag is needed.

Use `main-<shortsha>` for staging validation and release sign-off. Treat
`main-latest` as a lint/render convenience tag, not as sign-off material.

## Chart contract

`ci-helm.yml` validates `charts/tokenplace`, packages chart name `tokenplace`, and
publishes immutable OCI chart versions to `oci://ghcr.io/futuroptimist/charts/tokenplace`.
Before publishing, the workflow checks whether the exact chart version already
exists and fails instead of overwriting it.

The staging smoke render keeps the relay-specific guardrails active: one replica,
`Recreate` strategy, one worker, XDG paths under `/tmp`, no distributed relay URL
regression, and no duplicate environment variable names.

## Staging deploy flow

Run the deploy commands from a Sugarkube checkout, not from this token.place
repository.

1. Open the latest successful **Build and publish GHCR relay image** run for
   `.github/workflows/ci-image.yml` on `main`.
2. Copy the immutable tag from the workflow summary or the GHCR package page. It
   must look like `main-REPLACE_SHORTSHA`.
3. Confirm the Helm chart version you plan to deploy exists or publish it with
   the **Publish Helm chart** workflow (`.github/workflows/ci-helm.yml`).
4. Deploy from Sugarkube with the current app-specific recipe:

   ```bash
   just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
   ```

5. Once Sugarkube P5 lands, use the generic app recipe instead:

   ```bash
   just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
   ```

6. Verify rollout from the cluster:

   ```bash
   kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
   kubectl -n tokenplace get deploy,po,svc,ingress
   curl -fsS https://staging.token.place/livez
   curl -fsS https://staging.token.place/healthz
   ```

## Chart version checks

Use the chart version recorded in the Sugarkube token.place app config or version
file, then check GHCR before deployment:

```bash
CHART_VERSION=0.1.0
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version "$CHART_VERSION"
```

If the chart version does not exist, publish it by running `ci-helm.yml` for the
ref containing the desired `charts/tokenplace/Chart.yaml`. If the version exists
but does not contain the expected chart contents, stop; chart versions are
immutable and must not be overwritten.

## Local-development appendix

Local Docker builds are for developer smoke tests only. They are not part of the
Sugarkube staging or production release path.

```bash
docker build -t tokenplace-relay:local -f Dockerfile .
docker run --rm -p 127.0.0.1:5010:5010 \
  -e TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0 \
  tokenplace-relay:local
```
