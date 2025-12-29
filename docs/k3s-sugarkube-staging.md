# Deploying token.place relay to sugarkube staging

> **Scope:** Relay-only staging for `https://staging.token.place`. No GPU workers or `server.py`
> pods run in-cluster yet; the relay fronts external servers and serves the static UI.

This runbook mirrors the dspace staging guide at
[`dspace/docs/k3s-sugarkube-dev.md`](https://github.com/democratizedspace/dspace/blob/v3/docs/k3s-sugarkube-dev.md).
Follow it to publish the token.place relay image to GHCR, wire Cloudflare for
`staging.token.place`, and deploy via the sugarkube Helm helpers. The commands below assume the
standard Raspberry Pi HA cluster documented in the sugarkube repo.

## Published artifacts

- Relay OCI image: `ghcr.io/democratizedspace/tokenplace-relay`
  - Tags published from `main`: `main-<shortsha>`, `main-latest`
  - Workflow: `.github/workflows/relay-oci.yml`
- Helm chart: reuse `deploy/charts/tokenplace-relay` in this repo (or port it into sugarkube).
  - Service port: `5010`
  - Health/readiness path: `/healthz`

## Prerequisites

- Access to the sugarkube HA cluster with `kubectl` and `helm` configured.
- `just` installed on the node where you will run sugarkube recipes.
- Ability to pull from `ghcr.io/democratizedspace` with the GitHub token you will use for Helm.
- Cloudflare manages the `token.place` DNS zone and you can create routes + records.

Cluster baseline (follow the sugarkube docs):

- Traefik ingress installed and healthy:
  [`Install and verify Traefik ingress`](https://github.com/futuroptimist/sugarkube/blob/main/docs/raspi_cluster_operations.md#install-and-verify-traefik-ingress)
- Cloudflare Tunnel connector running and ready:
  [`cloudflare_tunnel.md`](https://github.com/futuroptimist/sugarkube/blob/main/docs/cloudflare_tunnel.md)

## Cloudflare: stage `staging.token.place`

Mirror the dspace tunnel setup with the token.place hostname:

1. In Cloudflare Zero Trust **Networks → Tunnels**, open the connector used for sugarkube.
2. Add a **Published application route**:
   - **Hostname**: `staging.token.place`
   - **Service type**: `HTTP`
   - **Service URL**: `traefik.kube-system.svc.cluster.local`
3. Confirm Cloudflare created a proxied DNS record:
   - **Type**: `CNAME`
   - **Name**: `staging`
   - **Target**: `<tunnel-uuid>.cfargotunnel.com`
   - **Proxy status**: Proxied (orange cloud)

TLS terminates at Cloudflare; Traefik inside the cluster handles plaintext HTTP. Keep SSL/TLS
settings aligned with the dspace doc (Flexible/Full, no custom page rules required).

## Build and publish the relay image

Pushes to `main` run `.github/workflows/relay-oci.yml`, which builds `Dockerfile.relay` and pushes:

- `ghcr.io/democratizedspace/tokenplace-relay:main-latest`
- `ghcr.io/democratizedspace/tokenplace-relay:main-<shortsha>`

To force a build, trigger the workflow manually from the Actions tab (`workflow_dispatch` input
`branch=main`). No Helm chart is published automatically; the existing `deploy/charts/tokenplace-relay`
chart can be packaged inside sugarkube if needed.

### Cutting a deployment tag (optional)

If sugarkube values pin a specific image, pass `tag=main-<shortsha>` to the Just recipe (see below)
instead of relying on `main-latest`.

## Deploy via sugarkube (Helm)

Sugarkube exposes generic Helm helpers in the `justfile` (see
[`helm-oci-install`](https://github.com/futuroptimist/sugarkube/blob/main/justfile#L345-L346) and
`helm-oci-upgrade`). Mirror the dspace pattern with a token.place values file
`docs/examples/tokenplace.values.staging.yaml` in sugarkube (to be added there) that sets:

```yaml
image:
  repository: ghcr.io/democratizedspace/tokenplace-relay
  tag: main-latest

service:
  port: 5010

ingress:
  enabled: true
  className: traefik
  host: staging.token.place
  tls: false

env:
  TOKEN_PLACE_ENV: staging
  TOKEN_PLACE_RELAY_PUBLIC_URL: https://staging.token.place
```

Then deploy from `~/sugarkube`:

```bash
cd ~/sugarkube
just helm-oci-install \
  release=tokenplace-relay namespace=tokenplace \
  chart=oci://ghcr.io/democratizedspace/charts/tokenplace-relay \
  values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml \
  version_file=docs/apps/tokenplace-relay.version \
  default_tag=main-latest
```

(Adjust the chart location if you publish it to GHCR; otherwise package `deploy/charts/tokenplace-relay`
locally.)

### Runtime configuration knobs

Relay container (from `Dockerfile.relay` / `docker/relay/entrypoint.sh`):

- `RELAY_HOST` (default `0.0.0.0`)
- `RELAY_PORT` (default `5010`)
- `RELAY_WORKERS` / `RELAY_THREADS` / `RELAY_TIMEOUT` / `RELAY_GRACEFUL_TIMEOUT`
- `TOKEN_PLACE_RELAY_PUBLIC_URL` (advertises the public base URL in `/healthz`)

Server pods (when added later) can point at the relay without code changes using:

- `TOKENPLACE_RELAY_URL` or `TOKEN_PLACE_RELAY_URL` (defaults to `http://localhost`)
- `TOKENPLACE_RELAY_PORT` or `TOKEN_PLACE_RELAY_PORT` (defaults to `5000`)
- CLI flags `--relay_url` and `--relay_port` on `server.py` still work with these defaults.

## Verification

After deployment:

```bash
kubectl -n tokenplace get ingress,svc,pods
kubectl -n tokenplace rollout status deploy/tokenplace-relay --timeout=120s
kubectl -n tokenplace port-forward svc/tokenplace-relay 18080:5010
curl -s http://localhost:18080/healthz | jq
```

Success criteria:

- `/healthz` returns `status":"ok"` and includes `publicBaseUrl` when configured.
- Browsing `https://staging.token.place` returns the static UI.

### End-to-end (post server rollout)

Once `server.py` is configured with `TOKENPLACE_RELAY_URL=https://staging.token.place`, confirm
messages traverse relay → server → relay:

```bash
curl -s http://localhost:18080/next_server
# returns a server_public_key once a server is registered
```

## Troubleshooting

- Image pull errors: ensure GHCR login via `helm registry login ghcr.io -u <user> -p <token>`.
- Ingress unreachable: verify Cloudflare Tunnel route for `staging.token.place` and Traefik service
  in `kube-system`.
- Readiness flaps: check `/healthz` for `status:"draining"` during rollouts; Kubernetes should retry.
