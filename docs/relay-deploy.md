# token.place relay deployment

This guide describes how to deploy the `token.place` relay in a k3s cluster and connect it to the
GPU-backed `server.py` process running on a dedicated Windows 11 host with an RTX 4090.

> **Status note (April 2026):** Canonical migration sequencing now lives in
> [docs/roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md).
> For relay-on-sugarkube operator workflows, start with
> [docs/relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md) and the environment runbooks:
> [docs/k3s-sugarkube-dev.md](k3s-sugarkube-dev.md),
> [docs/k3s-sugarkube-staging.md](k3s-sugarkube-staging.md), and
> [docs/k3s-sugarkube-prod.md](k3s-sugarkube-prod.md).

## Canonical GHCR artifacts

Use the canonical token.place artifacts for Sugarkube staging and production:

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Relay Dockerfile: root `Dockerfile`
- Chart source: `charts/tokenplace`
- Chart OCI reference: `oci://ghcr.io/futuroptimist/charts/tokenplace`

The `ci-image.yml` workflow builds the root Dockerfile and publishes immutable tags on `main` and
semver tag pushes. Pull requests and manual dispatch runs validate only. For staging, copy the
`main-<shortsha>` immutable tag from the workflow summary or GHCR package page.

The `ci-helm.yml` workflow validates and publishes the `tokenplace` chart to GHCR. Chart versions
are immutable: the publish job refuses to overwrite an existing version.

## Sugarkube deployment workflow

Run these commands from a Sugarkube checkout, not from token.place.

1. Find a successful token.place `ci-image.yml` run on `main`.
2. Copy the immutable image tag, for example `main-deadbee`.
3. Confirm the chart version with `ci-helm.yml` or GHCR.
4. Deploy with the current app-specific Sugarkube command:

```bash
just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
```

Once Sugarkube P5 lands, use the generic app command:

```bash
just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
```

Do not use local Docker builds or local chart paths for staging or production. See
[ops/sugarkube-release.md](ops/sugarkube-release.md) for the full release contract.

## Local-development image notes

Local image builds are for throwaway developer testing only. The canonical Dockerfile is at the repo
root:

```bash
docker build -t tokenplace-relay:dev -f Dockerfile .
```

For production-grade immutability, prefer the immutable `main-<shortsha>`, semver, or `sha-<shortsha>`
GHCR tags from GitHub Actions. Digest pinning is also supported by the chart:

```yaml
image:
  repository: ghcr.io/futuroptimist/tokenplace-relay
  digest: sha256:0123456789abcdef...
  tag: ""
```

When `image.digest` is supplied the Helm helper emits `repository@digest`. Falling back to
`image.tag` renders `repository:tag`, and leaving both empty resolves to the chart `appVersion`.

## Probes and graceful shutdown

Kubernetes continuously verifies the relay’s health:

- The readiness probe hits `GET /healthz` on the named `http` port every 10s after an initial 5s
  delay. During shutdown the probe fails, signalling Kubernetes to drain active connections.
- The liveness probe checks `GET /livez` on the same port starting 20s after startup, repeating every
  20s to ensure the process remains responsive.
- Pods define `terminationGracePeriodSeconds: 30` and a `preStop` hook that sends SIGTERM then sleeps
  briefly so connections can close cleanly before the container exits.

## Helm rendering checks

For local validation of the canonical chart source, render the chart with the same immutable tag you
plan to deploy from Sugarkube:

```bash
helm template tokenplace charts/tokenplace \
  --namespace tokenplace \
  --set ingress.enabled=true \
  --set ingress.host=staging.token.place \
  --set image.tag=main-REPLACE_SHORTSHA > /tmp/tokenplace-render.yaml
```

For a published chart version, render the OCI chart after replacing `CHART_VERSION` and the image
tag:

```bash
CHART_VERSION=0.1.0
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace \
  --version "$CHART_VERSION" \
  --namespace tokenplace \
  --set ingress.enabled=true \
  --set ingress.host=staging.token.place \
  --set image.tag=main-REPLACE_SHORTSHA > /tmp/tokenplace-render.yaml
```
