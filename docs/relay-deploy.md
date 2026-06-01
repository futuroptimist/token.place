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

## Container image

Multi-architecture relay images (linux/amd64 and linux/arm64) are published by
`.github/workflows/ci-image.yml` to GitHub Container Registry as
`ghcr.io/futuroptimist/tokenplace-relay`. Pull requests and manual dispatches
build and smoke-test only. Pushes to `main` publish `main-<shortsha>`,
`main-latest`, and `sha-<shortsha>`; semver tags `vX.Y.Z` publish `vX.Y.Z` and
`sha-<shortsha>`.

Use the workflow summary's immutable Sugarkube tag for staging and promotion,
for example:

```text
main-REPLACE_SHORTSHA
```

`main-latest` is available as a chart lint/render convenience but should not be
used as the staging or production promotion tag. See
[docs/ops/sugarkube-release.md](ops/sugarkube-release.md) for the full GHCR
image and chart contract.

If production later chooses digest pinning, set `image.digest` so the chart
renders `repository@digest`; otherwise deploy by immutable tag.

The container exposes port `5010` internally. Runtime environment variables:

- `RELAY_HOST` (default `0.0.0.0`)
- `RELAY_PORT` (default `5010`)
- `TOKENPLACE_GPU_HOST`/`TOKENPLACE_GPU_PORT` are injected only when the chart targets an external
  GPU hostname. Headless releases rely on the in-cluster DNS entry and derive their port from
  `TOKENPLACE_RELAY_UPSTREAM_URL`.
- `TOKENPLACE_RELAY_UPSTREAM_URL` defaults to `http://gpu-server:<port>`.

## Ingress, TLS, and certificates

The chart ships with Traefik defaults so a cluster using cert-manager can issue Let’s Encrypt
certificates automatically:

- `ingress.className` defaults to `traefik`.
- `ingress.annotations` already includes
  `cert-manager.io/cluster-issuer: letsencrypt-dns01`.
- Each environment must set `ingress.hosts[].host` for its FQDN and
  `ingress.tls[].secretName` for the certificate secret. The same host list should appear under both
  keys so cert-manager can provision the secret bound to the ingress.

Override these values in your environment-specific `values.yaml` files so staging, production, and
other clusters receive the expected routes and TLS secrets. The Helm values schema now enforces that
each host begins with `relay.` and that TLS entries include a `cert-manager.io/cluster-issuer`
annotation plus a non-empty secret so cert-manager can mint the certificate automatically.

## GPU indirection options

The relay reaches the GPU host through an indirection layer that you can control per environment:

- **ExternalName mode (default):** set `gpuExternalName.host` to the DNS name that resolves to your
  Windows host. The chart renders a `Service` named `gpu-server` of type `ExternalName` and injects
  `TOKENPLACE_GPU_HOST`/`TOKENPLACE_GPU_PORT` into the deployment so the relay connects directly to
  that hostname and port.
- **Headless Service + Endpoints:** set `gpuExternalName.useHeadless: true` (or
  `gpuExternalName.headless.enabled: true`) and provide static addresses via
  `gpuExternalName.headless.addresses`. The chart generates a headless `Service` with the supplied
  `Endpoints`. In this mode the relay resolves `gpu-server` inside the cluster and reuses the port
  from `TOKENPLACE_RELAY_UPSTREAM_URL`, so no GPU-specific environment overrides are required.

Whichever mode you choose, set `gpuExternalName.port` to the TCP port where `server.py` listens. The
default is `5015`, and the chart rewrites the upstream URL accordingly. You can override `upstream.url`
when pointing at a different scheme or host. For ExternalName deployments, tighten
`networkPolicy.externalNameCIDR` to the GPU host’s public IP (or CIDR) so only that address is
reachable from the relay pods. The packaged defaults ship with the reserved test-net placeholder
`192.0.2.42/32`, keeping the relay egress-locked until you provide the real destination.

## Probes and graceful shutdown

Kubernetes continuously verifies the relay’s health:

- The readiness probe hits `GET /healthz` on the named `http` port every 10s after an initial 5s
  delay. During shutdown the probe fails, signalling Kubernetes to drain active connections.
- The liveness probe checks `GET /livez` on the same port starting 20s after startup, repeating every
  20s to ensure the process remains responsive.
- Pods define `terminationGracePeriodSeconds: 30` and a `preStop` hook that sends SIGTERM then sleeps
  briefly so connections can close cleanly before the container exits.

## Helm deployment workflow

Sugarkube is the canonical deploy surface. The chart source is `charts/tokenplace`
and the OCI chart ref is `oci://ghcr.io/futuroptimist/charts/tokenplace`.
`ci-helm.yml` validates, packages, and publishes immutable chart versions; the
publish job refuses to overwrite an existing chart version.

1. Find a successful `ci-image.yml` run and copy the immutable Sugarkube tag from
   the workflow summary.
2. Confirm the chart version from `ci-helm.yml` is available:

   ```bash
   helm show chart oci://ghcr.io/futuroptimist/charts/tokenplace --version CHART_VERSION
   ```

3. Deploy from a Sugarkube checkout with the current app-specific wrapper:

   ```bash
   just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
   ```

4. Once Sugarkube P5 lands, use the generic app command:

   ```bash
   just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
   ```

5. After rollout, verify the health and readiness probes:

   ```bash
   kubectl -n tokenplace get deploy,po,svc,ingress
   kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
   curl -fsS https://staging.token.place/livez
   curl -fsS https://staging.token.place/healthz
   ```

Local Helm chart paths and local Docker images are for developer experiments
only; do not use `./deploy/charts/tokenplace-relay` or local image builds for
Sugarkube staging or production.

## Running `server.py` on Windows 11 (RTX 4090 host)

1. Install [Python 3.12](https://www.python.org/downloads/windows/) and ensure `py` is on your PATH.
2. Clone the repository onto the Windows host.
3. Create and activate a virtual environment:
   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
4. Install server dependencies:
   ```powershell
   pip install --upgrade pip
   pip install -r config/requirements_server.txt
   ```
5. If you use a relay registration token, export it before starting the server:
   ```powershell
   $Env:TOKEN_PLACE_RELAY_SERVER_TOKEN = "<secure-token>"
   ```
6. Start the server, binding to the port referenced by the Helm release (default `3000`):
   ```powershell
   python server.py --server_port 3000 --relay_port 5010
   ```
7. Allow inbound traffic on the chosen port through Windows Defender Firewall or any endpoint
   security software.
8. Confirm connectivity from the k3s cluster by resolving the DNS entry:
   ```bash
   kubectl -n tokenplace exec deploy/relay-tokenplace-relay -- \
     getent hosts gpu-server
   ```

## Network considerations

- The default `NetworkPolicy` denies all traffic except:
  - Ingress from the Traefik controller (namespace + label selector configured via
    `networkPolicy.traefik`).
  - Egress to kube-dns (UDP/TCP 53) when `networkPolicy.allowDNS` is true.
  - Egress to either the configured ExternalName host (via `networkPolicy.externalNameCIDR`, which
    defaults to the non-routable `192.0.2.42/32` placeholder) or the
    explicit headless IPs in `gpuExternalName.headless.addresses`.
  - Additional overrides supplied through `networkPolicy.extraIngress` / `networkPolicy.extraEgress`.
- The pod and container run as an unprivileged user (`1000`), drop all Linux capabilities, enforce a
  read-only root filesystem (an `emptyDir` is mounted at `/tmp`), disallow privilege escalation, and
  opt into the `RuntimeDefault` seccomp profile.
- Liveness and readiness probes target `/livez` and `/healthz` on the named `http` port. During
  shutdown the readiness probe reports a 503 so Kubernetes drains the pod before termination.
- If you operate behind cert-manager, ensure the ingress annotations reference your issuer (default
  `cert-manager.io/cluster-issuer: letsencrypt-dns01`).

With the Windows host advertising `server.py` on the expected port and the Helm release pointing to
that DNS record, pods inside the cluster can reach the GPU-backed server transparently through the
`gpu-server` service.
