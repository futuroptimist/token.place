# token.place relay task prompt

> One-click repo task: containerize relay.py, ship multi-arch images, helm-ize, and integrate with k3s
>
> You are the code agent for token.place. Implement the following **in one atomic PR**.
>
> CONTEXT
> - Architecture split: `server.py` runs on a Windows 11 box with an RTX 4090; `relay.py` runs inside the k3s cluster.
> - In-cluster services must call the GPU host via a **stable DNS name**. Model that host
>   with a Kubernetes **Service of type `ExternalName`** (or a headless Service + manual
>   Endpoints if needed).
> - Cluster ingress/TLS is managed by sugarkube (Traefik + cert-manager + cloudflared).
>
> DELIVERABLES
> 1) **Containerization**
>    - `Dockerfile` (non-root, minimal base; expose port via `ENV RELAY_PORT`).
>    - Graceful shutdown on SIGTERM.
>    - Health endpoints: `/healthz` (readiness), `/livez` (liveness).
>    - Structured JSON logs; optional `/metrics` (Prometheus counter for requests).
>
> 2) **Multi-arch build & publish**
>    - `.github/workflows/build.yml` using Docker Buildx to publish **linux/amd64 + linux/arm64** images to **GHCR**.
>    - Tags: semver (if applicable) and immutable `sha-<shortsha>`; set `org.opencontainers.image.*` labels.
>
> 3) **Helm chart (`deploy/charts/tokenplace-relay`)**
>    - `Deployment` with liveness/readiness probes, resource requests/limits appropriate for Pi 5.
>    - `Service` (ClusterIP).
>    - `Ingress` for `relay.<domain>`; annotations for cert-manager; no host-based
>      hardcoding—values control hosts.
>    - `values.yaml` includes:
>      - `gpuExternalName.host` (DNS name of the 4090 host)
>      - `upstream.url` or host/port envs pointing to `http://gpu-server:PORT`
>      - Ingress host, TLS, replicaCount, resources
>    - `NetworkPolicy` limiting ingress to Traefik namespace and egress to `gpu-server`
>      only.
>    - Optional `HPA` (CPU/mem).
>
> 4) **Kubernetes indirection to the GPU host**
>    - Create an **ExternalName Service** `gpu-server` in the app namespace:
> ```yaml
> apiVersion: v1
> kind: Service
> metadata:
>   name: gpu-server
>   namespace: tokenplace
> spec:
>   type: ExternalName
>   externalName: gpu-box.example.com   # set via Helm values
> ```
>    - Alternatively include a headless Service + manual Endpoints manifest behind a
>      values flag when an IP must be pinned.
>
> 5) **Docs**
>    - `docs/relay-deploy.md`: how to set `gpuExternalName.host`, expected ports, and
>      Windows steps to run `server.py`.
>    - `docs/prompts/codex/tokenplace-relay.md`: keep THIS prompt + the acceptance
>      checklist.
>
> ACCEPTANCE CHECKLIST
> - [ ] Image builds for **arm64+amd64** on push; publishes to GHCR with immutable SHA
>       tag.
> - [ ] Deployment becomes **Ready** with working liveness/readiness.
> - [ ] Ingress reachable at `relay.<env-domain>` with a valid cert (cert‑manager).
> - [ ] `gpu-server` ExternalName resolves and relay can reach `server.py` on the 4090
>       host.
> - [ ] Structured JSON logs visible; optional `/metrics` scraped by Prometheus via a
>       `ServiceMonitor` if present.
> - [ ] Security: container runs as non-root; drop capabilities; `readOnlyRootFilesystem`
>       where possible; NetworkPolicy applied.
