# token.place relay prompt

One-click repo task: containerize relay.py, ship multi-arch images, helm-ize, and integrate with k3s

## Deliverables

1. **Containerization**
   - `Dockerfile` (non-root, minimal base; expose port via `ENV RELAY_PORT`).
   - Graceful shutdown on SIGTERM with readiness turning red during drain.
   - Health endpoints: `/healthz` (readiness), `/livez` (liveness).
   - Structured JSON logs; optional `/metrics` (Prometheus counter for requests).

2. **Multi-arch build & publish**
   - `.github/workflows/build.yml` using Docker Buildx to publish **linux/amd64 + linux/arm64** images to **GHCR**.
   - Tags: semver (if applicable) and immutable `sha-<shortsha>`; set `org.opencontainers.image.*` labels including source/revision/created.
   - No implicit `:latest` push.

3. **Helm chart (`deploy/charts/tokenplace-relay`)**
   - `Deployment` with liveness/readiness probes, securityContext hardening (non-root UID 1000, drop ALL, read-only root, RuntimeDefault seccomp), and resource requests/limits appropriate for Pi 5.
   - `Service` (ClusterIP) and optional `ServiceMonitor` gated by values.
   - `Ingress` for `relay.<domain>`; annotations for cert-manager (`letsencrypt-dns01`); values control hosts/TLS.
   - `values.yaml` includes:
     - `image.repository/tag/digest/pullPolicy` (digest preferred for prod).
     - `gpuExternalName` settings for both ExternalName and headless+Endpoints indirection.
     - `serviceMonitor`, network policy overrides, ingress, autoscaling, etc.
   - `NetworkPolicy` default-denies, allowing ingress from Traefik and egress only to DNS + GPU target unless extra rules are provided.
   - Optional `HPA` (CPU/mem).

4. **Kubernetes indirection to the GPU host**
   - Provide an **ExternalName Service** `gpu-server` by default.
   - Toggle to a headless Service + manual Endpoints (addresses list) via values when IP pinning is required.
   - Deployment only exports `TOKENPLACE_GPU_HOST/PORT` when talking to an external hostname.

5. **Docs**
   - `docs/relay-deploy.md`: document digest pinning, GPU indirection modes, probe paths/ports, security defaults, ServiceMonitor toggle, NetworkPolicy behavior, and Windows steps to run `server.py`.
   - `docs/prompts/codex/tokenplace-relay.md`: keep THIS prompt + the acceptance checklist aligned with current requirements.

## Acceptance checklist

- [x] Image builds for **arm64+amd64** on push and publishes to GHCR
      with immutable SHA tag(s) and OCI labels
      (sha-* tags plus MIT license metadata enforced by tests).
- [ ] Values support digest pinning and render the helper-based image reference.
- [ ] Deployment becomes **Ready** with `/livez` + `/healthz` probes; readiness fails while draining on shutdown.
- [ ] Ingress reachable at `relay.<env-domain>` with a valid cert (cert‑manager).
- [ ] GPU indirection works for both ExternalName and headless+Endpoints modes; environment variables are set only when required.
- [ ] Structured JSON logs visible; optional `/metrics` scraped by Prometheus when the `ServiceMonitor` toggle is enabled.
- [ ] Security: container runs as UID 1000, drops capabilities, disallows privilege escalation, enforces RuntimeDefault seccomp & read-only root; NetworkPolicy default-denies except DNS + GPU target (overridable via values).
