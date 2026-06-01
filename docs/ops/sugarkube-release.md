# token.place Sugarkube release workflow

This runbook is the canonical GHCR-first release path for Sugarkube-managed token.place relay deployments.
It keeps staging/prod operators out of local Docker builds: GitHub Actions publishes the relay image,
GitHub Actions validates and publishes the Helm chart, and Sugarkube deploys immutable GHCR image tags with
an immutable OCI chart version.

## Canonical release artifacts

- Relay Dockerfile: `Dockerfile`
- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Helm chart source: `charts/tokenplace`
- Helm chart OCI reference: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Chart name: `tokenplace`

Do not use the deprecated local chart path `deploy/charts/tokenplace-relay` for Sugarkube staging or
production operations.

## Image publishing contract

The `ci-image.yml` workflow builds and smoke-tests the relay image on pull requests without publishing.
Only eligible pushes publish image tags:

- Push to `main` publishes:
  - `ghcr.io/futuroptimist/tokenplace-relay:main-REPLACE_SHORTSHA`
  - `ghcr.io/futuroptimist/tokenplace-relay:main-latest`
  - `ghcr.io/futuroptimist/tokenplace-relay:sha-REPLACE_SHORTSHA`
- Push of a semver tag such as `v0.1.0` publishes:
  - `ghcr.io/futuroptimist/tokenplace-relay:v0.1.0`
  - `ghcr.io/futuroptimist/tokenplace-relay:sha-REPLACE_SHORTSHA`
- `workflow_dispatch` builds a selected ref for validation only; it does not publish GHCR tags.

Use `main-REPLACE_SHORTSHA` for staging candidate validation. Use a semver tag such as `v0.1.0` only
after the candidate has been promoted through the project release process.

## Chart publishing contract

The `ci-helm.yml` workflow validates `charts/tokenplace`, renders staging-like smoke checks, packages
the chart, and publishes to `oci://ghcr.io/futuroptimist/charts/tokenplace` for non-PR runs in the
canonical `futuroptimist/token.place` repository. The publish job checks whether the target chart
version already exists and refuses to overwrite an existing OCI artifact.

Before a staged deploy, confirm the chart version you intend to use exists:

```bash
helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version REPLACE_CHART_VERSION
```

If the chart version does not exist yet, run or re-run `ci-helm.yml` from GitHub Actions for the
intended ref. Do not publish charts from a local workstation as part of the standard staging/prod path.

## Staging deployment path

Run these steps from GitHub Actions and a Sugarkube checkout; do not run them from a local token.place
Docker build.

1. Find a successful `ci-image.yml` run for the candidate commit on `main`.
2. Copy the immutable `main-REPLACE_SHORTSHA` tag from the workflow summary or the GHCR package page.
3. Confirm that `ci-helm.yml` has validated and published the desired chart version.
4. Deploy from a Sugarkube checkout with the current app-specific recipe:

   ```bash
   just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
   ```

5. Once the generic Sugarkube app recipes land, use the equivalent app contract command:

   ```bash
   just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
   ```

6. Verify rollout and public health:

   ```bash
   kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
   kubectl -n tokenplace get deploy,po,svc,ingress
   curl -fsS https://staging.token.place/livez
   curl -fsS https://staging.token.place/healthz
   ```

## Release tag deployment path

After a candidate is approved and the repository publishes a semver tag, deploy the release tag rather
than a moving convenience tag:

```bash
just tokenplace-oci-deploy env=staging tag=vX.Y.Z
```

When generic Sugarkube app recipes are available:

```bash
just app-deploy app=tokenplace env=staging tag=vX.Y.Z
```

Production onboarding is intentionally out of scope for this PR. Use production commands only after
the project has an approved production runbook and release sign-off.

## Local-development appendix

Local Docker builds are still useful for development and troubleshooting, but they are not the
staging/prod release path:

```bash
docker build -t tokenplace-relay:local -f Dockerfile .
docker run --rm -p 127.0.0.1:5010:5010 -e TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0 tokenplace-relay:local
```
