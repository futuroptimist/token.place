# Relay on Sugarkube onboarding (token.place)

This guide defines the **current near-term Sugarkube model** for token.place.

## Scope and architecture (current)

Sugarkube scope is **relay-only**:

- In cluster: `relay.py` only.
- Out of cluster: `server.py`, desktop Tauri compute nodes, Macs, Windows PCs, Raspberry Pi
  GPU/AI hat nodes, and other compute nodes.
- No in-cluster backend/GPU service is required for this phase.

The relay is **operationally stateful today** because registrations, queued client messages, and
replies are held in process memory. Current operating model:

- one pod
- one Gunicorn worker
- one replica
- accepted state loss if the pod restarts or is replaced

Multi-replica + shared state (Redis or similar) is explicitly future work and out of scope for
this phase.

Note on upgrades: because relay is deployed via Kubernetes `Deployment`, default `RollingUpdate`
can briefly run more than one pod during upgrades. If strict one-pod behavior is required, enforce
single-pod rollout settings (for example `Recreate` or `maxSurge=0`) in Sugarkube values.

## Artifact ownership and source of truth

token.place publishes deployable artifacts; Sugarkube owns environment values, wrappers, and
operator workflow.

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- OCI Helm chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Preferred deploy tag for staging/prod validation: immutable `main-<shortsha>`
- `main-latest` is convenience-only and not production sign-off material

> The `tokenplace.*` values/version files in command examples below are **Sugarkube-owned future
> contract artifacts** expected after follow-up Sugarkube prompts land.

## Default hostnames

- Staging default: `https://staging.token.place`
- Production default: `https://token.place`

Operators can override hostnames in Sugarkube values and Cloudflare route configuration.
Cloudflare Tunnel still handles public DNS/edge routing, while Helm values control the in-cluster
Ingress and TLS spec rendered for Traefik.

## Sugarkube deployment command patterns

> Run the following from a **Sugarkube checkout**, not from token.place.

First install pattern:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Existing release upgrade pattern:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Production pattern uses `docs/examples/tokenplace.values.prod.yaml` with the same approved
immutable tag.

## Validation (staging example)

```bash
helm template tokenplace oci://ghcr.io/futuroptimist/charts/tokenplace --version "$(grep -E '^[0-9]+\.[0-9]+\.[0-9]+' docs/apps/tokenplace.version | head -n1)" --namespace tokenplace -f docs/examples/tokenplace.values.dev.yaml -f docs/examples/tokenplace.values.staging.yaml --set image.tag=main-REPLACE_SHORTSHA > /tmp/tokenplace-staging-render.yaml
grep -n "tls:" -A6 /tmp/tokenplace-staging-render.yaml
grep -n "staging.token.place" /tmp/tokenplace-staging-render.yaml
grep -n "tokenplace-staging-tls" /tmp/tokenplace-staging-render.yaml
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace get ingress tokenplace -o yaml
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
curl -vI https://staging.token.place/
```

For production validation, render with `docs/examples/tokenplace.values.prod.yaml`, then verify
`token.place` + `tokenplace-prod-tls` in `Ingress.spec.tls`, and use `curl -vI https://token.place/`.

Optional note: true relay traffic validation requires a registered external compute node and an
E2EE client-flow probe (for example encrypted `/api/v1/chat/completions`).

## Guardrails

- Keep API v1 relay-blind E2EE invariants intact (ciphertext only + safe routing metadata).
- Staging/prod overlays must set `ingress.tls.enabled: true` so chart output includes
  `Ingress.spec.tls` for `staging.token.place`/`token.place`.
- Assumes cert-manager is installed and `letsencrypt-dns01` is available as `ClusterIssuer`.
- Do not treat legacy relay endpoints as active production path.
- Do not require `TOKENPLACE_RELAY_UPSTREAM_URL` for relay-only Sugarkube readiness.
- Do not use local chart path deployment (`./deploy/charts/tokenplace-relay`) for Sugarkube
  steady-state operations.
