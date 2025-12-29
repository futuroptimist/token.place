# Deploying token.place relay to sugarkube staging

> **Scope:** Deploy only `relay.py` to `staging.token.place` on the sugarkube HA k3s
> cluster. The GPU workers (`server.py`) stay external for now.

This runbook mirrors the layout of the dspace staging guide
([docs/k3s-sugarkube-dev.md](https://github.com/democratizedspace/dspace/blob/v3/docs/k3s-sugarkube-dev.md))
and calls out the Cloudflare + Traefik tunnel expectations used by sugarkube.

## Source of truth and upstream references

- Relay Helm chart in this repo: [`k8s/charts/tokenplace-relay`](../k8s/charts/tokenplace-relay)
- Sugarkube deployment guide for dspace (structure to mirror):
  [docs/apps/dspace.md](https://github.com/futuroptimist/sugarkube/blob/main/docs/apps/dspace.md)
- Cloudflare Tunnel walkthrough:
  [cloudflare_tunnel.md](https://github.com/futuroptimist/sugarkube/blob/main/docs/cloudflare_tunnel.md)
- GHCR image workflow for token.place relay: `.github/workflows/relay-oci.yml`

## Published artifacts

- Relay image: `ghcr.io/democratizedspace/tokenplace-relay`
  - Tags from pushes to `main`: `main-<shortsha>` and `main-latest`
  - Use the immutable `main-<shortsha>` tag for pinned rollouts; `main-latest` is the moving
    default for quick redeploys.
- Helm chart (local for now): [`k8s/charts/tokenplace-relay`](../k8s/charts/tokenplace-relay)
  - Chart service port: `5010`
  - Health endpoint: `GET /healthz` (200 when ready, 503 when draining)

## Assumptions and prerequisites

- Sugarkube three-node HA k3s cluster is online (`env=dev`) with Traefik installed per the
  sugarkube operations guide.
- You can log in to GHCR with a token that can pull `ghcr.io/democratizedspace` packages.
- Cloudflare manages DNS for `token.place` and you can create a tunnel route + DNS record.
- Tooling on the control node: `kubectl`, `helm`, `just`, and GHCR credentials.

## Cloudflare: staging.token.place

Mirror the dspace tunnel pattern, substituting the token.place hostname:

1. In Cloudflare Zero Trust → Networks → Tunnels, open the connector for your cluster.
2. Add a **Published application route**:
   - **Hostname:** `staging.token.place`
   - **Service type:** `HTTP`
   - **Service URL:** `traefik.kube-system.svc.cluster.local`
3. Confirm DNS shows a proxied CNAME:
   - **Name:** `staging`
   - **Target:** `<tunnel-UUID>.cfargotunnel.com`
   - **Proxy status:** Proxied (orange cloud)
4. Keep TLS termination at Cloudflare. The in-cluster Traefik ingress remains HTTP.

## How the image is published

- Pushes to `main` (or a manual **Run workflow**) trigger
  [`relay-oci.yml`](../.github/workflows/relay-oci.yml).
- The workflow builds `Dockerfile.relay` for `linux/amd64` and `linux/arm64`, tags it as
  `main-<shortsha>` and `main-latest`, and pushes to GHCR.
- To cut a pinned tag for deployment, copy the `main-<shortsha>` from the workflow logs or
  `crane ls ghcr.io/democratizedspace/tokenplace-relay | grep main-`.

## Deploying via sugarkube

The sugarkube repo should carry values files such as
`docs/examples/tokenplace-relay.values.staging.yaml` plus a Just recipe (for example,
`just tokenplace-relay-oci-redeploy`) that wraps `helm-oci-install`,
mirroring the dspace helpers. Until that lands, use the generic Helm OCI helpers:

```bash
cd ~/sugarkube
just helm-oci-install \
  release=tokenplace-relay namespace=tokenplace-relay \
  chart=/opt/projects/token.place/k8s/charts/tokenplace-relay \
  values=docs/examples/tokenplace-relay.values.dev.yaml,docs/examples/tokenplace-relay.values.staging.yaml \
  default_tag=main-latest
```

- Override `default_tag` with `tag=main-<shortsha>` to pin a specific image.
- Expect the Service to listen on port `5010` and the readiness probe to hit `/healthz`.

## Runtime configuration knobs (staging vs. prod)

- Relay container (via environment variables):
  - `RELAY_HOST` (default `0.0.0.0`)
  - `RELAY_PORT` (default `5010`, override to `8080`+ if your ingress expects it)
  - `TOKEN_PLACE_RELAY_PUBLIC_URL` or `RELAY_PUBLIC_URL` to surface the public URL in `/healthz`
  - `TOKENPLACE_RELAY_UPSTREAM_URL` to point the relay at an alternate GPU server endpoint
- GPU workers (`server.py`):
  - `TOKEN_PLACE_RELAY_URL` to point at `https://staging.token.place`
  - `TOKEN_PLACE_RELAY_PORT` to match the ingress port (443 for Cloudflare HTTPS, or 5010/8080
    if connecting within the cluster)
- Helm values (chart defaults):
  - `service.port` and `relay.port` default to `5010`
  - Probes hit `/healthz`

## Verification

1. Check pod and ingress status:
   ```bash
   kubectl -n tokenplace-relay get pods,svc,ingress
   ```
2. Verify the health endpoint:
   ```bash
   curl -fsS https://staging.token.place/healthz | jq
   ```
3. Load https://staging.token.place in a browser and confirm the static UI renders.
4. After configuring GPU workers with `TOKEN_PLACE_RELAY_URL=https://staging.token.place` and the
   matching port, observe `/sink` traffic to ensure messages round-trip through the relay.

## Troubleshooting

- If `/healthz` returns 503 with `"status": "draining"`, Kubernetes is terminating the pod; wait for
  the new replica.
- Name resolution failures for `gpu_host` appear as `"gpuHostResolution": "failed"` in `/healthz`;
  confirm DNS for the upstream GPU endpoint.
- GHCR pull errors: `helm registry login ghcr.io -u <user> -p <token>` on the control node and retry.
