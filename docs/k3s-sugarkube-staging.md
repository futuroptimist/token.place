# Deploying token.place relay to k3s with sugarkube (staging)

> **Scope:** Relay-only staging at `staging.token.place` on the sugarkube HA k3s cluster.
> The GPU-backed `server.py` continues to run outside the cluster for now.

This runbook mirrors the [dspace staging guide][dspace-v3] so you can onboard token.place
quickly without hunting for prereqs. It links to the GHCR workflow and Cloudflare steps
required to expose the relay through Traefik; Helm packaging for sugarkube is called out as a
follow-up in that repo.

## Source of truth and upstream references

- GHCR build workflow for the relay image:
  [.github/workflows/relay-oci.yml](../.github/workflows/relay-oci.yml)
- Sugarkube dspace guide for Cloudflare Tunnel and Traefik:
  [docs/apps/dspace.md](https://github.com/futuroptimist/sugarkube/blob/main/docs/apps/dspace.md)
  (follow the same ingress + tunnel pattern for `staging.token.place`)
- dspace staging playbook (reference for style and expectations):
  [docs/k3s-sugarkube-dev.md][dspace-v3]

## Published artifacts

- Relay container image (GHCR): `ghcr.io/democratizedspace/tokenplace-relay`
  - Tags pushed from `main`: `main-latest`, `main-<shortsha>`, and `sha-<shortsha>` (documented in
    [.github/workflows/relay-oci.yml](../.github/workflows/relay-oci.yml))
- Relay Helm chart (already present in this repo):
  [`deploy/charts/tokenplace-relay`](../deploy/charts/tokenplace-relay)
  - Sugarkube will publish this chart as an OCI artifact in its repo; until then, helm install
    directly from this path when testing locally.
  - Default container port: `5010`

## Assumptions and prerequisites

- Sugarkube HA3 cluster is online with Traefik installed per
  [raspi_cluster_operations.md][raspi-ops].
- You can log in to GHCR with a token that can pull from `ghcr.io/democratizedspace`.
- Cloudflare manages `token.place` and you can create a published application route for
  `staging.token.place` that targets `traefik.kube-system.svc.cluster.local` (proxied).
- `kubectl`, `helm`, and `just` are available on the node where you run sugarkube commands.

### Staging domain

- Public URL: `https://staging.token.place`
- TLS termination: Cloudflare Tunnel → Traefik (no in-cluster TLS required)

### Hardware and cluster layout

- Raspberry Pi 5 HA3 cluster (identical assumptions to the dspace staging guide)
- Target environment for sugarkube commands: `env=dev`

## Cloudflare setup for `staging.token.place`

Mirror the dspace tunnel configuration, but substitute the `token.place` zone and hostname:

1. **Create a published application route**
   - Zone: `token.place`
   - Hostname: `staging.token.place`
   - Service type: `HTTP`
   - Service URL: `traefik.kube-system.svc.cluster.local`
   - Keep the route proxied so traffic flows through the tunnel and Traefik sees
     `Host: staging.token.place`.
2. **Verify DNS**
   - Confirm Cloudflare created a proxied CNAME:
     - Type: `CNAME`
     - Name: `staging`
     - Target: `<tunnel-UUID>.cfargotunnel.com`
     - Proxy status: Proxied (orange cloud)
3. **TLS mode**
   - Set SSL/TLS encryption mode to **Full** (matching dspace) so Cloudflare keeps TLS to the tunnel
     but Traefik continues to speak HTTP to the relay Service.
   - No cert-manager issuer is required for this staging host.

If you need step-by-step tunnel setup, follow sugarkube's [cloudflare_tunnel.md][cf-tunnel].

## How deployments work

1. **Publishing images**
   - Every push to `main` builds and pushes the relay image to GHCR with tags
     `main-latest`, `main-<shortsha>`, and `sha-<shortsha>`.
   - Trigger manually via the **Build and publish relay image** workflow in GitHub if needed.
2. **Helm deployment via sugarkube**
   - Sugarkube will consume the existing Helm chart (`deploy/charts/tokenplace-relay`).
   - A follow-up **in the sugarkube repo** will add a values file (for example,
     `docs/examples/tokenplace-relay.values.staging.yaml`) plus a `just tokenplace-oci-redeploy`
     recipe mirroring `just dspace-oci-redeploy` that targets:
     - Chart: `oci://ghcr.io/democratizedspace/charts/tokenplace-relay` (will be published from
       sugarkube once the OCI flow is wired)
     - Image: `ghcr.io/democratizedspace/tokenplace-relay:main-latest` by default
     - Ingress host: `staging.token.place`
   - Until the sugarkube recipe lands, you can helm-install directly from this repo's chart:
     ```bash
     helm upgrade --install tokenplace-relay ./deploy/charts/tokenplace-relay \
       --namespace tokenplace --create-namespace \
       --set image.repository=ghcr.io/democratizedspace/tokenplace-relay \
       --set image.tag=main-latest \
       --set ingress.enabled=true \
       --set ingress.className=traefik \
       --set ingress.hosts[0].host=staging.token.place \
       --set service.port=80 --set containerPort=5010 \
       --set upstream.url=http://gpu-server:5015
     ```
   - When sugarkube support is in place, prefer the `just` wrapper so tags, chart versions, and
     ingress hosts stay consistent (that recipe will ship alongside the values file in sugarkube).
3. **Cutting deployment tags**
   - Pin immutable builds with `main-<shortsha>` (or `sha-<shortsha>`). Mutable `main-latest`
     is fine for initial staging smoke tests; force a rollout restart after upgrading if you reuse
     `main-latest`.

## Runtime configuration knobs

- Relay container (Kubernetes)
  - `RELAY_HOST` (default `0.0.0.0`)
  - `RELAY_PORT` (default `5010`; set to `8080` if you prefer the Kubernetes service to expose 8080)
  - `TOKENPLACE_RELAY_PUBLIC_URL` / `TOKEN_PLACE_RELAY_PUBLIC_URL` / `RELAY_PUBLIC_URL` (optional;
    surfaced in `/healthz` for diagnostics and UI config)
  - Readiness endpoint: `GET /healthz` (200 when ready, 503 with `Retry-After: 0` while draining)
- GPU server (runs outside the cluster for now)
  - `TOKENPLACE_RELAY_URL` / `TOKEN_PLACE_RELAY_URL` / `RELAY_URL` /
    `TOKENPLACE_RELAY_UPSTREAM_URL` / `TOKEN_PLACE_RELAY_UPSTREAM_URL` to point at the staging relay
    (for example, `https://staging.token.place`)
  - `TOKENPLACE_RELAY_PORT` / `TOKEN_PLACE_RELAY_PORT` / `RELAY_PORT` if the relay listens on a
    non-default port (set `443`
    when using HTTPS without an explicit port)
  - `TOKEN_PLACE_RELAY_SERVER_TOKEN` if you require a registration token between server and relay

## Verification

After deploying, validate end-to-end reachability:

1. Confirm pods and ingress:
   ```bash
   kubectl -n tokenplace get pods
   kubectl -n tokenplace get ingress
   ```
2. Health check through the ingress:
   ```bash
   curl -fsS https://staging.token.place/healthz | jq
   ```
3. Static UI sanity check:
   ```bash
   curl -I https://staging.token.place/
   ```
4. (After server.py is pointed at the staging relay) validate relay registration and flow:
   ```bash
   # From the GPU host
   TOKENPLACE_RELAY_URL=https://staging.token.place TOKENPLACE_RELAY_PORT=443 \
     python server.py --server_port 3000 --relay_port 443 --relay_url https://staging.token.place
   # Watch server logs for successful /sink polling and client request handling
   ```

## Follow-ups for sugarkube

- Add a staged values file and `just tokenplace-oci-redeploy` recipe that mirrors
  `just dspace-oci-redeploy`, targeting the token.place relay chart and GHCR image.
- Publish the Helm chart as an OCI artifact under
  `ghcr.io/democratizedspace/charts/tokenplace-relay` from the sugarkube repo (or add a local chart
  reference there until OCI publishing is enabled).
- Wire the Cloudflare Tunnel hostname into sugarkube docs/examples once the values file is added.

[dspace-v3]: https://github.com/democratizedspace/dspace/blob/v3/docs/k3s-sugarkube-dev.md
[raspi-ops]: https://github.com/futuroptimist/sugarkube/blob/main/docs/raspi_cluster_operations.md
[cf-tunnel]: https://github.com/futuroptimist/sugarkube/blob/main/docs/cloudflare_tunnel.md
