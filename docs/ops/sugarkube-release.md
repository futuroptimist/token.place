# Sugarkube release workflow for token.place

This is the canonical release path for deploying token.place on Sugarkube. It is
GHCR-first: GitHub Actions builds the relay image from the repository-root
`Dockerfile`, GitHub Actions validates and publishes the Helm chart from
`charts/tokenplace`, and Sugarkube deploys an immutable image tag with an OCI
chart version.

## Canonical artifacts

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Relay image workflow: `.github/workflows/ci-image.yml`
- Helm chart source: `charts/tokenplace`
- Helm chart workflow: `.github/workflows/ci-helm.yml`
- OCI chart ref: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Chart name: `tokenplace`

The retired image workflows (`build.yml` and `relay-oci.yml`) are removed. Do
not publish or deploy the retired democratizedspace relay package for
token.place Sugarkube environments.

## Image tag contract

Pull requests build and smoke-test the relay image but do not publish. Manual
`workflow_dispatch` image runs also validate only; publish by pushing to `main`
or by pushing a semver tag.

Pushes to `main` publish all of these tags:

```text
ghcr.io/futuroptimist/tokenplace-relay:main-<shortsha>
ghcr.io/futuroptimist/tokenplace-relay:main-latest
ghcr.io/futuroptimist/tokenplace-relay:sha-<shortsha>
```

Semver Git tags `vX.Y.Z` publish these tags:

```text
ghcr.io/futuroptimist/tokenplace-relay:vX.Y.Z
ghcr.io/futuroptimist/tokenplace-relay:sha-<shortsha>
```

Use `main-<shortsha>` or `vX.Y.Z` for Sugarkube deploys. `main-latest` exists
only as a chart lint/render convenience and a quick human smoke-test pointer;
it is not the staging or production promotion tag.

## Chart contract

`ci-helm.yml` validates `charts/tokenplace`, packages it, and publishes the
chart to GHCR as an immutable OCI artifact. The publish job refuses to overwrite
an existing chart version.

Before deploying, confirm the chart version you intend to use:

```bash
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version CHART_VERSION
```

Replace `CHART_VERSION` with the version recorded in the successful
`ci-helm.yml` run summary or in the Sugarkube app config/version file.

## Staging deploy path

Run these steps from the appropriate repositories and replace every placeholder
before copying commands.

1. In GitHub Actions, open **Build and publish GHCR relay image**
   (`ci-image.yml`) and find a successful `main` or `vX.Y.Z` run.
2. Copy the immutable Sugarkube tag from the workflow summary. For a `main` run
   it has this shape:

   ```text
   main-REPLACE_SHORTSHA
   ```

3. In GitHub Actions, open **Publish Helm chart** (`ci-helm.yml`) and confirm the
   desired chart version was validated and published. If needed, run it before
   deploying.
4. From a Sugarkube checkout, deploy the current app-specific wrapper:

   ```bash
   just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
   ```

5. Once the generic Sugarkube app recipes from P5 land, use the uniform command:

   ```bash
   just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
   ```

For semver release validation, replace `main-REPLACE_SHORTSHA` with the release
tag, for example `v0.1.0`.

## Local-development appendix

Local image builds are for developer iteration only. They are not the staging or
production release path.

```bash
docker build -t tokenplace-relay:dev -f Dockerfile .
docker run --rm -p 127.0.0.1:5010:5010 \
  -e TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0 \
  tokenplace-relay:dev
```

Use GitHub Actions and GHCR for deployable Sugarkube artifacts.
