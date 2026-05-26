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

> The `tokenplace.*` values/version files in command examples below are Sugarkube contract
> artifacts used to render ingress + TLS consistently.

## Default hostnames

- Staging default: `https://staging.token.place`
- Production default: `https://token.place`

Operators can override hostnames in Sugarkube values and Cloudflare route configuration.


## Ingress TLS contract for staging/prod

Cloudflare Tunnel still owns public DNS/Tunnel routing to Traefik; Helm does **not** manage
Cloudflare routes. Sugarkube values must explicitly enable chart TLS rendering per environment:

- staging overlay: `ingress.tls.enabled: true` + `ingress.tls.secretName: tokenplace-staging-tls`
- prod overlay: `ingress.tls.enabled: true` + `ingress.tls.secretName: tokenplace-prod-tls`
- both overlays keep Traefik ingress class and `cert-manager.io/cluster-issuer` annotation

Assumption: the referenced cert-manager ClusterIssuer exists in the target cluster and can mint
certificates for the configured hostnames.

Operators should verify the rendered Ingress `spec.tls` block before deploy.

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
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

For production validation, replace the host with `https://token.place`.

Optional note: true relay traffic validation requires a registered external compute node and an
E2EE client-flow probe (for example encrypted `/api/v1/chat/completions`).

## Guardrails

- Keep API v1 relay-blind E2EE invariants intact (ciphertext only + safe routing metadata).
- Do not treat legacy relay endpoints as active production path.
- Do not require `TOKENPLACE_RELAY_UPSTREAM_URL` for relay-only Sugarkube readiness.
- Do not use local chart path deployment (`./deploy/charts/tokenplace-relay`) for Sugarkube
  steady-state operations.
