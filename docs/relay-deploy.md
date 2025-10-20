# token.place relay deployment

This guide describes how to deploy the `token.place` relay in a k3s cluster and connect it to the
GPU-backed `server.py` process running on a dedicated Windows 11 host with an RTX 4090.

## Container image

Multi-architecture images (linux/amd64 and linux/arm64) are published to GitHub Container Registry
as `ghcr.io/<org-or-user>/tokenplace-relay`. Each build is tagged with both an immutable
`sha-<shortsha>` and any matching semver tag.

The container exposes port `5010` internally. Runtime environment variables:

- `RELAY_HOST` (default `0.0.0.0`)
- `RELAY_PORT` (default `5010`)
- `TOKENPLACE_GPU_HOST` (defaults to the Kubernetes service name `gpu-server`)
- `TOKENPLACE_GPU_PORT` (default `3000`)
- `TOKENPLACE_RELAY_UPSTREAM_URL` (default `http://gpu-server:3000`)

## Configuring the GPU host DNS entry

The relay contacts the GPU server through a stable DNS record. In Helm, set
`values.gpuExternalName.host` to the DNS name that resolves to your Windows host. The chart will
create a `Service` named `gpu-server` of type `ExternalName` pointing to that DNS entry. If you need
an IP-based indirection (for example, when the upstream requires a static IP and DNS is not
available), set `gpuExternalName.useHeadless: true` and provide `gpuExternalName.ip` to ship a
headless `Service` with static `Endpoints`.

The default upstream TCP port is `3000`. This must match the port where `server.py` listens on the
Windows host. Update `gpuExternalName.port` (and `upstream.url`) in `values.yaml` if you expose the
Windows service on a different port.

## Helm deployment workflow

1. Add the chart directory to your Helm repository or package it locally.
2. Prepare an override file (for example, `relay-values.yaml`) with environment-specific values:
   ```yaml
   image:
     repository: ghcr.io/example/tokenplace-relay
     tag: sha-<shortsha>
   ingress:
     hosts:
       - host: relay.staging.example.com
         paths:
           - path: /
             pathType: Prefix
     tls:
       - secretName: relay-staging-tls
         hosts:
           - relay.staging.example.com
   gpuExternalName:
     host: gpu-box.example.com
     port: 3000
   upstream:
     url: http://gpu-server:3000
   networkPolicy:
     allowedEgressCIDRs:
       - 203.0.113.42/32  # optional when you know the GPU host address
   ```
3. Deploy with Helm:
   ```bash
   helm upgrade --install relay ./deploy/charts/tokenplace-relay \
     --namespace tokenplace --create-namespace \
     -f relay-values.yaml
   ```
4. After rollout, verify the health and readiness probes:
   ```bash
   kubectl -n tokenplace get pods -l app.kubernetes.io/name=tokenplace-relay
   kubectl -n tokenplace get ingress relay-tokenplace-relay
   ```
5. Optional: enable the `ServiceMonitor` by setting `serviceMonitor.enabled: true` when a Prometheus
   operator is available in the cluster.

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

- The chart ships with a `NetworkPolicy` that permits ingress only from the Traefik namespace (set
  via `networkPolicy.traefikNamespace`). Adjust `additionalIngressSelectors` if other in-cluster
  clients must reach the relay.
- Egress is limited to DNS (when `networkPolicy.allowDNS` is true) and to the GPU host IPs provided
  via `gpuExternalName.ip` or `networkPolicy.allowedEgressCIDRs`.
- If you operate behind cert-manager, ensure the ingress annotations reference your issuer (for
  example, `cert-manager.io/cluster-issuer: letsencrypt-production`).

With the Windows host advertising `server.py` on the expected port and the Helm release pointing to
that DNS record, pods inside the cluster can reach the GPU-backed server transparently through the
`gpu-server` service.
