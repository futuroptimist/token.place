# Kubernetes Manifests for token.place Relay

These manifests run `relay.py` as a Deployment and expose it via a Service.

1. Build and push the Docker image:
   ```bash
   docker build -t tokenplace-relay:latest -f docker/Dockerfile.relay .
   # push to your registry or import to k3s
   ```
2. Apply the manifests:
   ```bash
   kubectl apply -f k8s/
   ```

Edit `relay-deployment.yaml` to point `image:` at your registry if needed.

## Sugarkube Helm bundle

The [`sugarkube`](https://github.com/futuroptimist/sugarkube) Pi image applies
Helm bundles from `/etc/sugarkube/helm-bundles.d/` once the k3s cluster reports
`Ready`. This repository now ships a chart under
[`k8s/charts/tokenplace-relay/`](charts/tokenplace-relay) plus a matching bundle
definition in [`k8s/sugarkube/`](sugarkube/).

Copy the bundle into place on a sugarkube host to deploy the relay via Helm:

```bash
sudo cp k8s/sugarkube/token-place.env \
  /etc/sugarkube/helm-bundles.d/token-place.env
sudo cp k8s/sugarkube/token-place-values.yaml \
  /opt/sugarkube/helm-values/token-place-values.yaml
```

The env file targets the in-repo chart at `/opt/projects/token.place` and waits
for `deployment.apps/tokenplace-relay` to roll out before marking the bundle as
healthy. Override image details or resource settings by editing the values file
after copying it.

## Raspberry Pi pod manifest

For a Raspberry Pi k3s cluster, use `relay-raspi-pod.yaml` to run a single
ARM64 pod. The manifest now passes `--host 0.0.0.0 --port 5010` and sets
`TOKEN_PLACE_ENV=production` so the Service and observability stacks can reach it:

```bash
kubectl apply -f k8s/relay-raspi-pod.yaml
```

The deployment manifest includes resource requests/limits and basic health
probes. Adjust these values according to your cluster capacity.
