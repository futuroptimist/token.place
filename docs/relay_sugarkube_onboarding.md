# Relay on sugarkube onboarding (token.place)

This guide explains the near-term token.place deployment split for sugarkube.

## Scope and architecture (current phase)

Sugarkube scope is **relay-only** and deploys `relay.py` only.

- In-cluster on sugarkube: `relay.py`.
- Out-of-cluster (external compute plane): `server.py`, desktop Tauri compute nodes,
  Macs, Windows PCs, Raspberry Pi GPU/AI hats, and other compute nodes.
- No in-cluster backend/GPU service is required in this phase.

The relay is technically stateful today because server registrations, client messages, and
server replies are kept in process memory. Operationally this means:

- one pod
- one Gunicorn worker
- one replica
- accepted state loss when the pod exits/restarts

Redis/shared in-memory database and multi-replica relay architecture are future work and out of
scope for this phase.

## Artifact ownership and references

token.place publishes the runtime artifacts consumed by sugarkube operators:

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Canonical chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`

Tag guidance:

- Preferred for staging/prod sign-off: immutable `main-<shortsha>`.
- `main-latest` is convenience-only and not production sign-off material.

## Hostnames

Default hostnames for this phase:

- staging: `https://staging.token.place`
- production: `https://token.place`

Operators may override hostnames in sugarkube values and Cloudflare routes/tunnels.

## Sugarkube command patterns (run from sugarkube checkout)

Run the following from a **sugarkube repository checkout**, not from token.place.

First install pattern:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Existing release upgrade pattern:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Production pattern uses `docs/examples/tokenplace.values.prod.yaml` with the same approved
immutable `main-<shortsha>` tag.

Sugarkube-specific token.place wrappers are expected to exist as follow-up work; these generic OCI
helpers remain valid in the meantime.

## Validation

Staging checks:

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

Production uses the same checks with `https://token.place`.

## Guardrails

- Keep relay-blind E2EE invariant: relay state/logs carry ciphertext plus safe routing metadata
  only.
- Keep API v1 as the active runtime path for `v0.1.0` and keep it non-streaming.
- Do not treat legacy relay routes as active production path guidance.
