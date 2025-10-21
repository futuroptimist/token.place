# token.place relay deployment

This guide describes how to deploy the `token.place` relay in a k3s cluster and connect it to the
GPU-backed `server.py` process running on a dedicated Windows 11 host with an RTX 4090.

## Container image

Multi-architecture images (linux/amd64 and linux/arm64) are published to GitHub Container Registry
as `ghcr.io/<org-or-user>/tokenplace-relay`. Each build is tagged with both an immutable
`sha-<shortsha>` and any matching semver tag.

Prefer pinning releases by digest in production to guarantee immutability and eliminate the risk of
tag reuse:

```yaml
image:
  repository: ghcr.io/example/tokenplace-relay
  digest: sha256:0123456789abcdef...
  tag: ""  # leave empty when digest is provided so the chart renders `repo@digest`
```

When `image.digest` is supplied the Helm helper emits `repository@digest`. Falling back to
`image.tag` renders `repository:tag`, and leaving both empty resolves to the chart `appVersion`.
Digest pinning avoids supply-chain surprises and should be the default for production releases.

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
other clusters receive the expected routes and TLS secrets.

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
reachable from the relay pods.

## Probes and graceful shutdown

Kubernetes continuously verifies the relay’s health:

- The readiness probe hits `GET /healthz` on the named `http` port every 10s after an initial 5s
  delay. During shutdown the probe fails, signalling Kubernetes to drain active connections.
- The liveness probe checks `GET /livez` on the same port starting 20s after startup, repeating every
  20s to ensure the process remains responsive.
- Pods define `terminationGracePeriodSeconds: 30` and a `preStop` hook that sends SIGTERM then sleeps
  briefly so connections can close cleanly before the container exits.

## Helm deployment workflow

1. Add the chart directory to your Helm repository or package it locally.
2. Prepare an override file (for example, `relay-values.yaml`) with environment-specific values:
   ```yaml
   image:
     repository: ghcr.io/example/tokenplace-relay
     digest: sha256:0123456789abcdef...
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
     port: 5015
   upstream:
     url: http://gpu-server:5015
   serviceMonitor:
     enabled: true
     namespaceSelector:
       matchNames:
         - monitoring
   networkPolicy:
     extraEgress:
       - to:
           - ipBlock:
               cidr: 203.0.113.42/32
         ports:
           - protocol: TCP
             port: 5015
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
5. Optional: enable the `ServiceMonitor` by setting `serviceMonitor.enabled: true`. Labels default to
   `{ release: kube-prometheus-stack }`, interval `30s`, and path `/metrics` on the `http` port so the
   kube-prometheus-stack discovers the metrics endpoint without extra overrides.

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
  - Egress to either the configured ExternalName host (via `networkPolicy.externalNameCIDR`) or the
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
