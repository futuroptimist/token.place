# token.place relay deployment

This guide describes how to deploy the token.place relay in a k3s cluster. For Sugarkube staging
and production, use the GHCR-published relay image and OCI Helm chart instead of building a local
Docker image or installing a local chart path.

> **Status note (April 2026):** Canonical migration sequencing now lives in
> [docs/roadmap/desktop_compute_node_migration.md](roadmap/desktop_compute_node_migration.md).
> For relay-on-sugarkube operator workflows, start with
> [docs/relay_sugarkube_onboarding.md](relay_sugarkube_onboarding.md) and the environment runbooks:
> [docs/k3s-sugarkube-dev.md](k3s-sugarkube-dev.md),
> [docs/k3s-sugarkube-staging.md](k3s-sugarkube-staging.md), and
> [docs/k3s-sugarkube-prod.md](k3s-sugarkube-prod.md).

## Canonical Sugarkube image and chart workflow

Canonical artifacts:

- Relay Dockerfile: `Dockerfile`
- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Helm chart source: `charts/tokenplace`
- Helm chart OCI reference: `oci://ghcr.io/futuroptimist/charts/tokenplace`

GitHub Actions owns release publication. Pull requests build and smoke-test the relay image without
publishing. Pushes to `main` publish immutable `main-<shortsha>` and `sha-<shortsha>` tags plus the
`main-latest` convenience tag. Pushes of semver tags publish the matching `vX.Y.Z` tag and
`sha-<shortsha>`. Manual `workflow_dispatch` image runs validate only and do not publish.

Sugarkube staging deploys should follow the release runbook:

```bash
just tokenplace-oci-deploy env=staging tag=main-REPLACE_SHORTSHA
```

Once the generic Sugarkube app recipes land, the equivalent command is:

```bash
just app-deploy app=tokenplace env=staging tag=main-REPLACE_SHORTSHA
```

See [ops/sugarkube-release.md](ops/sugarkube-release.md) for the full release workflow, including
how to find the immutable image tag in the `ci-image.yml` workflow summary and confirm the chart
with `ci-helm.yml`.

## Helm values and digest pinning

The chart defaults to `ghcr.io/futuroptimist/tokenplace-relay:main-latest` only as a lint/render
convenience. Operators should override `image.tag` with an immutable workflow output such as
`main-REPLACE_SHORTSHA` for staging candidate validation or a semver release tag such as `v0.1.0`
after promotion.

For production hardening, you can also pin by digest when the deployment tooling supports it:

```yaml
image:
  repository: ghcr.io/futuroptimist/tokenplace-relay
  digest: sha256:0123456789abcdef...
  tag: ""
```

When `image.digest` is supplied the Helm helper emits `repository@digest`. Falling back to
`image.tag` renders `repository:tag`, and leaving both empty resolves to the chart `appVersion`.

## Relay-only runtime defaults

The canonical chart preserves the current relay-only Sugarkube semantics:

- one replica
- one Gunicorn worker
- `Recreate` rollout strategy for in-memory relay state
- read-only root filesystem with XDG write paths redirected to `/tmp`
- no in-cluster GPU/backend service required for relay readiness

The container exposes port `5010` internally. Runtime environment variables include `RELAY_HOST`
(default `0.0.0.0`) and `RELAY_PORT` (default `5010`).

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
