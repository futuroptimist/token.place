# token.place relay deployment guide

This guide describes how to run the `relay.py` service inside Kubernetes while forwarding
requests to the GPU-backed `server.py` process that remains on a dedicated Windows host.
It covers container configuration, Helm chart values, and the steps for preparing the
GPU machine.

## Container image

The relay container is published to GitHub Container Registry as
`ghcr.io/<org>/tokenplace-relay`. The image exposes port `5010` by default through the
`RELAY_PORT` environment variable and ships structured JSON logs, `/healthz`, `/livez`,
and `/metrics` endpoints. A Prometheus counter named `relay_http_requests_total`
increments for every request (excluding `/metrics`) so platform telemetry can track
per-method and per-endpoint usage. You can adjust runtime behaviour via the following
environment variables:

- `RELAY_PORT` – listener port (default `5010`).
- `RELAY_WORKERS` – number of Gunicorn workers (default `2`).
- `RELAY_LOG_LEVEL` – log level (`INFO` by default).
- `UPSTREAM_URL` – full URL that points at the GPU host (defaults to
  `http://gpu-server:5010`).

## Helm chart overview

Install the chart from `deploy/charts/tokenplace-relay`.

```bash
helm upgrade --install relay ./deploy/charts/tokenplace-relay \
  --namespace tokenplace --create-namespace \
  --set image.repository=ghcr.io/your-org/tokenplace-relay \
  --set image.tag=sha-<shortsha>
```

Key values to review:

- `gpuExternalName.host` – set this to the stable DNS name of the Windows GPU host.
- `gpuExternalName.name` – Kubernetes service name (defaults to `gpu-server`).
- `upstream.url` – override when the GPU service listens on a custom scheme/port. By
  default the chart points to `http://gpu-server:5010`.
- `ingress.hosts` and `ingress.tls` – configure ingress hostnames and cert-manager
  secrets for `relay.<environment-domain>`.
- `networkPolicy.egressCidrs` – CIDR blocks that are allowed for relay egress.
  Use the public or private IP of the GPU host (for example `203.0.113.7/32`).
- `gpuExternalName.headless` – enable this block when you must pin a specific IP. The
  chart will create a headless Service plus `Endpoints` manifest instead of an
  `ExternalName` record.
- `metrics.serviceMonitor.enabled` – set to `true` when a Prometheus Operator
  `ServiceMonitor` should scrape the `/metrics` endpoint.

Resource requests (`150m` CPU, `256Mi` memory) and limits (`500m`, `512Mi`) are tuned
for Raspberry Pi 5 class nodes. Adjust them if your cluster has different hardware.
The pod security context drops all Linux capabilities and mounts the root filesystem
read-only; an emptyDir volume is attached at `/tmp` for Gunicorn.

## ExternalName service

By default the chart creates an `ExternalName` Service named `gpu-server` that resolves
inside the cluster to the DNS value provided via `gpuExternalName.host`. When you must
pin the GPU host IP address, enable `gpuExternalName.headless.enabled` and supply at
least one endpoint in `gpuExternalName.headless.endpoints` to generate a headless
Service and matching Endpoints manifest.

After deploying the chart, run the following command to verify the indirection:

```bash
kubectl run -n tokenplace dns-test --rm -it --image=ghcr.io/your-org/tokenplace-relay \
  --restart=Never --command -- nslookup gpu-server
```

## Preparing the Windows GPU host

1. Install Python 3.11+ and the dependencies listed in `requirements.txt`.
2. Set the `TOKEN_PLACE_ENV` and any provider-specific secrets required by `server.py`.
3. Open PowerShell and launch the GPU server:
   ```powershell
   python server.py --host 0.0.0.0 --port 5010
   ```
4. Ensure the Windows firewall allows inbound TCP connections on the chosen port
   (5010 by default).
5. Create a DNS record (for example `gpu-box.example.com`) that points to the Windows
   machine so the cluster can reach it via the `gpu-server` ExternalName.

Keep `server.py` running whenever the cluster relay is active. If you need supervised
startup, configure the Windows Task Scheduler or NSSM to run the PowerShell command at
boot.

## Troubleshooting

- **Readiness probe failing** – check the pod logs for JSON-formatted health events and
  confirm that the GPU host DNS name resolves from inside the cluster. During shutdown
  the `/healthz` endpoint reports HTTP `503` with status `terminating`, which is
  expected once the pod receives `SIGTERM`.
- **NetworkPolicy blocks traffic** – update `networkPolicy.egressCidrs` with the correct
  IP address or enable the headless service mode. Do not forget to allow outbound DNS by
  leaving `networkPolicy.allowDns` set to `true` unless your cluster resolves DNS via a
  different service.
- **TLS issues** – verify cert-manager is issuing certificates for the ingress hosts and
  that the Traefik namespace label selector matches your installation.
