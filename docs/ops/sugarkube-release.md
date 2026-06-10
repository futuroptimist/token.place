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

Use `main-<shortsha>` or `vX.Y.Z` for Sugarkube deploys. `main-latest`,
`latest`, `staging`, `prod`, and `production` are mutable/convenience labels;
they must be rejected by Sugarkube deploy/promotion checks and are not staging
or production promotion tags. If Sugarkube keeps `docs/apps/tokenplace.prod.tag`
empty, production promotion must supply an explicit immutable tag on the command
line; populating that file is a deliberate release-management decision, not a
default.

## Chart contract

`ci-helm.yml` validates `charts/tokenplace` and packages it on pull requests,
main-branch pushes, and manual dispatches. Pushes to `main` publish to GHCR only
when chart source files changed and the `charts/tokenplace/Chart.yaml` version is
not already present as an immutable OCI artifact. If the chart source changed but
the version already exists, the workflow fails with a version-bump message; if no
chart source changed, an already-published version is a successful no-op. Manual
dispatches are validate/package-only unless the `publish` input is set.

Before deploying, confirm the chart version you intend to use:

```bash
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version CHART_VERSION
```

Replace `CHART_VERSION` with the version recorded in the successful
`ci-helm.yml` run summary or in the Sugarkube app config/version file. The
current token.place chart source is `0.1.1`; Sugarkube `docs/apps/tokenplace.version`
should pin `0.1.1` unless an operator explicitly documents why a different
package is being held back.

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
tag, for example `v0.1.0`. Generic HTTP checks are necessary but insufficient:
staging may not be promoted from those checks alone. A real external
desktop/compute node must register to staging, appear in `/healthz` and
`/relay/diagnostics`, and complete an encrypted API v1
relay/desktop-bridge E2EE request/response. Capture the immutable image tag,
chart version and digest where available, rendered or live deployment YAML,
health/diagnostics output after the compute test, and relay logs after the
compute test. The exact compute-node launch command is operator/environment
specific, so record the command actually used rather than inventing one here.

## Production promotion gate

Production uses the same GHCR image and OCI Helm chart path, with release and
namespace `tokenplace/tokenplace`, host `token.place`, and an explicit immutable
image tag. After promotion, repeat the real external proof against production:

- A separate production desktop/compute node registers to `https://token.place`
  and appears in `/healthz` and `/relay/diagnostics`.
- A real encrypted API v1 relay/desktop-bridge E2EE request/response succeeds
  through that registered production node.
- Plaintext relay-dispatched API v1 paths are intentionally fail-closed and are
  not production readiness evidence.
- Evidence includes the immutable image tag, chart version and digest where
  available, rendered or live deployment YAML, health/diagnostics output after
  the compute test, and relay logs after the compute test.

Cloudflare Tunnel/DNS/WAF routing is external to Helm and remains an external
release gate because desktop/compute-node registration can be blocked before it
reaches `relay.py`; validate HTTPS `/`, `/livez`, `/healthz`, and
`/relay/diagnostics`, and check Cloudflare Security Events by `cf-ray` when a
desktop/compute node sees a pre-app 403 before changing relay code.

## Local-development appendix

Local image builds, root `docker-compose.yml`, and raw `k8s/` manifests are for
developer iteration or legacy compatibility only. They are not the staging or
production Sugarkube release path.

```bash
docker build -t tokenplace-relay:dev -f Dockerfile .
docker run --rm -p 127.0.0.1:5010:5010 \
  -e TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0 \
  tokenplace-relay:dev
```

Use GitHub Actions, the GHCR relay image, and the OCI Helm chart for deployable
Sugarkube artifacts. The production relay remains intentionally single-pod,
one-worker, and in-memory for registrations, queues, and replies; state loss on
pod restart/replacement is accepted for this phase, and HA/durable queues are future work.
