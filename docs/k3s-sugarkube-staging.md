# Deploying token.place relay to k3s with sugarkube (staging)

> **Scope:** Staging traffic for `https://staging.token.place` runs **relay.py only**. No GPU
> workers or `server.py` pods run in-cluster yet. Production on `https://token.place` stays
> unchanged.

This runbook mirrors the structure of the dspace staging guide
([docs/k3s-sugarkube-dev.md](https://github.com/democratizedspace/dspace/blob/v3/docs/k3s-sugarkube-dev.md))
so sugarkube operators can deploy the relay from GHCR without digging through multiple repos.

## Source of truth

- Relay image: `ghcr.io/democratizedspace/tokenplace-relay`
- Image workflow: [`.github/workflows/relay-oci.yml`](../.github/workflows/relay-oci.yml)
- Helm chart (in-repo): [`deploy/charts/tokenplace-relay`](../deploy/charts/tokenplace-relay)
- Sugarkube deployment patterns: [sugarkube docs/apps/dspace.md](https://github.com/futuroptimist/sugarkube/blob/main/docs/apps/dspace.md)
  and the shared Helm helpers `helm-oci-install` / `helm-oci-upgrade` in the sugarkube justfile.

## Published artifacts and tags

- Pushes to `main` build and publish multi-arch (amd64 + arm64) images to GHCR:
  - Mutable tag: `ghcr.io/democratizedspace/tokenplace-relay:main-latest`
  - Immutable tag: `ghcr.io/democratizedspace/tokenplace-relay:main-<shortsha>`
  - Branch anchor: `ghcr.io/democratizedspace/tokenplace-relay:main`
- To pin a rollout, use the `main-<shortsha>` tag emitted by the workflow run. `main-latest` tracks
  the newest `main` build and is fine for fast redeploys.

## Prerequisites

- Sugarkube HA k3s cluster online with Traefik installed
  ([raspi_cluster_operations.md#install-and-verify-traefik-ingress](https://github.com/futuroptimist/sugarkube/blob/main/docs/raspi_cluster_operations.md#install-and-verify-traefik-ingress)).
- Access to the Cloudflare zone for `token.place`.
- `kubectl`, `helm`, and `just` available on the sugarkube control node.
- GHCR token with `read:packages` scope that can pull from `ghcr.io/democratizedspace`.

## Cloudflare: staging.token.place

Configure the Cloudflare Tunnel route exactly like `staging.democratized.space`, but targeting the
token.place hostname:

1. In the Cloudflare dashboard, open the `token.place` zone → **Zero Trust → Networks → Tunnels**.
2. Select the tunnel used for the cluster (reuse the one backing dspace if shared).
3. **Published application route** → **Add a published application route**:
   - **Hostname**: subdomain `staging`, domain `token.place`, empty path.
   - **Service**: Type `HTTP`, URL `traefik.kube-system.svc.cluster.local`.
4. Save the route; Cloudflare will create a DNS record automatically.
5. Verify DNS: **DNS → Records** should show a proxied CNAME
   `staging.token.place -> <tunnel-UUID>.cfargotunnel.com` (orange cloud enabled).
6. TLS mode: Cloudflare terminates TLS. Traefik can listen over HTTP inside the cluster (no
   cert-manager setup required for this host unless you enable end-to-end TLS later).

## Build and publish the relay image

- Default path: push to `main` and let the workflow run.
- Manual: trigger **Build and publish relay image** from the Actions tab (workflow_dispatch), leaving
  `ref=main` unless you need a feature branch.

## Deploy to sugarkube (Helm)

Use the sugarkube Helm helpers to keep parity with dspace. Until a dedicated sugarkube values file is
added, create one alongside the dspace examples (for example,
`docs/examples/tokenplace-relay.values.staging.yaml`) containing the host, image, and port:

```yaml
image:
  repository: ghcr.io/democratizedspace/tokenplace-relay
  tag: main-latest

service:
  port: 5010

env:
  TOKEN_PLACE_ENV: staging
  TOKENPLACE_RELAY_PUBLIC_URL: https://staging.token.place
```

Deploy with Helm via the sugarkube justfile (replace the `values` path with wherever you commit the
overrides in the sugarkube repo):

```bash
cd ~/sugarkube
just helm-oci-install \
  release=tokenplace-relay namespace=tokenplace-relay \
  chart=./deploy/charts/tokenplace-relay \
  values=docs/examples/tokenplace-relay.values.staging.yaml \
  default_tag=main-latest
```

- `default_tag` supplies `main-latest`. Pass `tag=main-<shortsha>` to pin a rollout.
- If you publish the chart to GHCR later, swap `chart=./deploy/charts/tokenplace-relay` for
  `chart=oci://ghcr.io/democratizedspace/charts/tokenplace-relay`.
- The relay listens on `0.0.0.0:${RELAY_PORT}` (default 5010). The health endpoint is `/healthz` and
  returns `503` with `status=draining` when SIGTERM is received, making it safe for Kubernetes probes.

## Runtime configuration (staging vs prod)

Relay-side environment knobs (defaults preserve current production behaviour):

| Variable | Default | Purpose |
| --- | --- | --- |
| `RELAY_HOST` | `127.0.0.1` (overridden to `0.0.0.0` in the container) | Bind address |
| `RELAY_PORT` | `5010` | Listener port |
| `TOKENPLACE_RELAY_PUBLIC_URL` / `RELAY_PUBLIC_URL` | _(unset)_ | Advertised base URL; echoed in `/healthz` for ingress/certificate sanity checks |
| `TOKENPLACE_RELAY_UPSTREAM_URL` | `http://gpu-server:3000` | Upstream GPU endpoint for legacy deployments (unused in staging) |
| `TOKENPLACE_RELAY_SERVER_TOKEN` | _(unset)_ | Optional shared secret for compute nodes calling `/sink` and `/source` |

Server / compute-node knobs (when you later point `server.py` at the hosted relay):

| Variable | Default | Purpose |
| --- | --- | --- |
| `TOKENPLACE_RELAY_URL` / `RELAY_URL` | `http://localhost` | Relay base URL (supports `https://staging.token.place`) |
| `TOKENPLACE_RELAY_PORT` / `RELAY_PORT` | `5000` | Relay port; parsed from `TOKENPLACE_RELAY_URL` if provided |
| `TOKEN_PLACE_ENV` | `development` | Controls debug logging and model defaults |

## Verification checklist

1. Pods healthy:

   ```bash
   kubectl -n tokenplace-relay get pods,svc,ingress
   ```

2. Health endpoint:

   ```bash
   curl -fsSL https://staging.token.place/healthz | jq
   ```

   Expect `status: "ok"` plus `publicUrl`.

3. Static UI:

   ```bash
   curl -I https://staging.token.place/
   ```

   Returns `200 OK` and serves the bundled relay UI.

4. (When servers register) confirm relay logs show `/sink` heartbeats and `/healthz` reports
   `knownServers > 0`.

## Follow-ups outside this repo

- Add a sugarkube values file and just recipe for `tokenplace-relay` mirroring the `dspace-oci-*`
  helpers so deployments stay copy/paste-able.
- Optionally publish the Helm chart as an OCI artifact
  (`oci://ghcr.io/democratizedspace/charts/tokenplace-relay`) once the sugarkube recipe expects it.
