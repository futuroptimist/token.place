# Raspberry Pi k3s Cluster Deployment

This guide explains how to run the `relay.py` service on a Raspberry Pi k3s cluster. It assumes you have multiple Raspberry Pi boards and want to manage them with k3s (a lightweight Kubernetes distribution).

For recommended hardware, see [Raspberry Pi Deployment Bill of Materials](RPI_BILL_OF_MATERIALS.md).

## 1. Prepare the Hardware

- Use Raspberry Pi 5 boards for best performance (Pi 4 also works).
- Ensure your network switch provides Power over Ethernet (PoE) or use PoE HATs. Otherwise, power each Pi with USB-C.
- Install Raspberry Pi OS 64‑bit on each device and enable SSH.

## 2. Install k3s

On the node you want to act as the control plane, run:

```bash
curl -sfL https://get.k3s.io | sh -
```

Retrieve the node token, which workers will use to join:

```bash
sudo cat /var/lib/rancher/k3s/server/node-token
```

On each worker Pi, join the cluster:

```bash
curl -sfL https://get.k3s.io | K3S_URL=https://<CONTROL_PLANE_IP>:6443 K3S_TOKEN=<NODE_TOKEN> sh -
```

Verify all nodes are ready:

```bash
kubectl get nodes
```

## 3. Build the relay image

On a machine with Docker (can be one of the Pis):

```bash
# From the repository root
docker build -t tokenplace-relay:latest -f docker/Dockerfile.relay .
```

Push the image to a registry accessible by your cluster or load it directly on each node. For a local cluster, you can import the image:

```bash
k3s ctr images import tokenplace-relay:latest
```

## 4. Deploy the relay

Apply the Kubernetes manifests provided in the `k8s/` directory:

```bash
kubectl apply -f k8s/
```

This creates a `Deployment` and `Service` exposing the relay on port 5000. Adjust the service type in `k8s/relay-service.yaml` if you need NodePort or LoadBalancer.

## 5. Verify

Check that the pod is running and accessible:

```bash
kubectl get pods
kubectl get svc tokenplace-relay
```

You can now connect to the relay's service IP from your network or expose it further using Cloudflare as described in [docs/RPI_RELAY_RUNBOOK.md](docs/RPI_RELAY_RUNBOOK.md).

