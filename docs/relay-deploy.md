# Deploying the token.place Relay

This guide explains how to run the `relay.py` service inside Kubernetes while the GPU-backed
`server.py` process remains on a separate Windows 11 workstation. The Helm chart located at
`deploy/charts/tokenplace-relay` packages all Kubernetes resources required for the relay.

## 1. Prepare the GPU workstation (Windows 11)

1. Install Python 3.12 and Git for Windows.
2. Clone the repository and install the server dependencies:
   ```powershell
   git clone https://github.com/tokenplace/token.place.git
   cd token.place
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r config/requirements_server.txt
   ```
3. Expose the relay registration token and listening port:
   ```powershell
   $env:TOKEN_PLACE_SERVER_TOKEN = "<strong-random-token>"
   $env:HOST = "0.0.0.0"
   $env:PORT = "5010"
   ```
4. Start the GPU server:
   ```powershell
   python server.py --host $env:HOST --port $env:PORT
   ```
5. Allow inbound traffic to the configured port (5010 by default) in Windows Defender
   Firewall so the cluster can reach the workstation.

## 2. Publish a stable DNS entry for the GPU box

Create or reuse a DNS record that always resolves to the GPU workstation. This host name is
referenced by the Helm chart and is exposed inside the cluster via an `ExternalName` service
named `gpu-server`.

## 3. Configure Helm values

Create a custom `values.yaml` for your environment. The critical settings are:

```yaml
gpuExternalName:
  host: gpu-box.example.com      # DNS name created in step 2

networkPolicy:
  gpuServerCIDRs:
    - 203.0.113.42/32            # Replace with the workstation's routable IP

upstream:
  host: gpu-server
  port: 5010
  scheme: http

ingress:
  hosts:
    - host: relay.example.net    # Domain routed through Traefik + cert-manager
      paths:
        - path: /
          pathType: Prefix
  tls:
    - hosts: [relay.example.net]
      secretName: relay-example-net-tls
```

If you already manage the server registration token inside a secret, reference it with:

```yaml
relay:
  serverToken:
    existingSecret: tokenplace-relay-token
    key: token
```

Optionally enable the headless service mode when you must pin static IP endpoints instead of
DNS:

```yaml
gpuExternalName:
  headless:
    enabled: true
    ports:
      - name: relay
        port: 5010
    addresses:
      - 203.0.113.42
```

## 4. Install or upgrade the chart

```bash
helm upgrade --install relay deploy/charts/tokenplace-relay \
  --namespace tokenplace --create-namespace \
  -f my-values.yaml
```

The Deployment exposes readiness at `/healthz`, liveness at `/livez`, and Prometheus metrics at
`/metrics`. The NetworkPolicy restricts ingress to the Traefik namespace and egress to the GPU
workstation plus Kubernetes DNS.

## 5. Validate connectivity

1. Confirm the ExternalName resolves inside the cluster:
   ```bash
   kubectl exec -n tokenplace deploy/relay-tokenplace-relay -- nslookup gpu-server
   ```
2. Ensure the relay pod can connect to the workstation:
   ```bash
   kubectl exec -n tokenplace deploy/relay-tokenplace-relay \
     -- curl -sf http://gpu-server:5010/livez
   ```
3. Verify Traefik exposes the ingress at `https://relay.<env-domain>` with a valid certificate.

With these steps the relay will proxy requests from Kubernetes workloads to the GPU-backed
`server.py` process running on Windows.
