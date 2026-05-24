# Relay on sugarkube onboarding (token.place)

This guide explains how and why to run `relay.py` on sugarkube for the current deployment phase.

## Scope and architecture (current phase)

Sugarkube scope is **relay-only** in this phase.

- In-cluster runtime: `relay.py` only.
- External compute/runtime components: `server.py`, desktop Tauri compute nodes, Macs,
  Windows PCs, Raspberry Pi GPU/AI hats, and other compute nodes.
- No in-cluster backend/GPU service is required for this phase.

The relay is operationally lightweight, but it is technically stateful today because
registrations, queued client messages, and replies are held in process memory.

Current operational model:

- one pod
- one Gunicorn worker
- one replica

State loss on pod death is accepted for now. Redis/in-memory database backed state,
replicated relay designs, and multi-replica high availability are future work and out of scope
for this phase.

## Canonical deployment artifacts

token.place publishes and owns the relay deployment artifacts:

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Helm chart (OCI): `oci://ghcr.io/futuroptimist/charts/tokenplace`

Tag guidance:

- Preferred for staging/prod validation: immutable `main-<shortsha>`.
- Convenience only: mutable `main-latest` (not for production sign-off).

## Platform ownership split

- token.place owns relay runtime code and publishes the relay image/chart.
- sugarkube owns environment values files, version pin files, release orchestration recipes,
  and operator runbooks.

## Hostnames

Default hostnames used by runbooks:

- Staging: `https://staging.token.place`
- Production: `https://token.place`

Operators may override hostnames in sugarkube values and Cloudflare routes.

## Sugarkube deployment commands

Run these commands from a **sugarkube checkout** (not from token.place):

First install pattern:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Existing release upgrade pattern:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Production promotion pattern (use prod values and same approved immutable tag):

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.prod.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Sugarkube-specific `tokenplace` wrapper recipes may also exist after the sugarkube follow-up
prompts. Those wrappers should call the same OCI chart with the same immutable tag discipline.

## Validation commands

Staging baseline checks:

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

For production, replace `https://staging.token.place` with `https://token.place`.

## Guardrails

- Keep API v1 relay architecture guardrails and relay-blind E2EE invariants intact.
- Do not assume legacy relay routes are active production paths.
- Do not require `TOKENPLACE_RELAY_UPSTREAM_URL` for relay-only sugarkube readiness.
- Do not require `gpuExternalName` placeholders for this phase.
- Do not use local chart paths such as `./deploy/charts/tokenplace-relay` for sugarkube
  steady-state deployment.

## Environment runbooks

- [k3s-sugarkube-dev.md](k3s-sugarkube-dev.md)
- [k3s-sugarkube-staging.md](k3s-sugarkube-staging.md)
- [k3s-sugarkube-prod.md](k3s-sugarkube-prod.md)
