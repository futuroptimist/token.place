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

For a Raspberry Pi k3s cluster, use `relay-raspi-pod.yaml` to run a single ARM64 pod:

```bash
kubectl apply -f k8s/relay-raspi-pod.yaml
```

The deployment manifest includes resource requests/limits and basic health
probes. Adjust these values according to your cluster capacity.
