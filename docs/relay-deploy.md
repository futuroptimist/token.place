# token.place relay deployment guide

This guide explains how to run the relay service inside a k3s cluster while forwarding
requests to the Windows host that executes `server.py` on the RTX 4090.

## Container image configuration

The relay image is published to GitHub Container Registry (`ghcr.io`). The build pipeline
creates multi-architecture images (`linux/amd64` and `linux/arm64`) and tags every push
with `sha-<shortsha>`. When you cut a semver tag, the workflow publishes the tag as well.

To run the image manually, set `RELAY_PORT` to the listener port (default: `5010`) and
optionally provide tuning variables for Gunicorn:

```bash
docker run --rm \
  -e RELAY_PORT=5010 \
  -p 5010:5010 \
  ghcr.io/<owner>/tokenplace-relay:sha-abcdef1
```

The container emits structured JSON logs and exposes the following endpoints:

- `/healthz`: readiness probe (503 while shutting down or if configuration cannot be loaded)
- `/livez`: liveness probe (503 only during shutdown)
- `/metrics`: Prometheus metrics (request counter plus standard Flask exporter data)

## Helm chart values

The Helm chart lives in `deploy/charts/tokenplace-relay`. Key values to review before
installing:

- `gpuExternalName.host`: **required** DNS name for the GPU workstation. This becomes the
  target of the `gpu-server` `ExternalName` service used inside the cluster.
- `gpuExternalName.port`: exposed port on the Windows host (defaults to `8000`).
- `gpuExternalName.headless.enabled`: switch to a headless `Service` plus manual
  `Endpoints` when you must pin a static IP instead of a DNS name. Set
  `gpuExternalName.headless.ip` to the 4090 host address.
- `env.upstream.url`: base URL the relay uses for outbound calls. The default points to
  `http://gpu-server:8000`, which resolves through the `ExternalName` service above.
- `ingress.hosts[*].host`: hostname routed by Traefik (for example `relay.mycluster.dev`).
- `ingress.tls`: TLS secret configuration handled by cert-manager.
- `networkPolicy.gpuServer.cidrs`: CIDR blocks that represent the GPU host. Because
  Kubernetes NetworkPolicy cannot restrict by DNS name, provide the resolved IP (for
  example `203.0.113.10/32`). The policy always allows DNS lookups to kube-system.
- `resources`: the default requests/limits fit a Raspberry Pi 5 class node. Adjust for
  your environment as needed.

### Installing the chart

1. Create a values override file (`relay-values.yaml`) with the correct hostnames and
   network CIDRs.
2. Install or upgrade the release:

   ```bash
   helm upgrade --install relay deploy/charts/tokenplace-relay \
     --namespace tokenplace \
     --create-namespace \
     -f relay-values.yaml
   ```

3. Verify probes:

   ```bash
   kubectl get deploy relay -n tokenplace
   kubectl get ingress relay -n tokenplace
   kubectl get svc gpu-server -n tokenplace
   ```

When Prometheus Operator is present, enable `.Values.serviceMonitor.enabled` so metrics are
scraped automatically.

## Windows RTX 4090 host setup

1. Install Python 3.11 on the Windows 11 workstation and clone the repository.
2. Create a virtual environment and install the server dependencies:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install --upgrade pip
   pip install -r config\requirements_server.txt
   ```

3. Expose `server.py` on a routable interface and port (for example `0.0.0.0:8000`):

   ```powershell
   $env:PYTHONUNBUFFERED = "1"
   py server.py --host 0.0.0.0 --port 8000
   ```

4. Ensure the workstation DNS name (for example `gpu-box.example.com`) matches
   `gpuExternalName.host` in the Helm chart and that the firewall allows inbound traffic on
   the configured port.

With this setup the in-cluster relay will reach the GPU host via the stable
`gpu-server.tokenplace.svc.cluster.local` address provided by the `ExternalName` service.
