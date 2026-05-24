# Relay on sugarkube onboarding (token.place)

This guide explains the relay-only deployment model for sugarkube using token.place-owned OCI
artifacts.

## Scope and architecture (relay-only)

Sugarkube scope for this phase is `relay.py` only.

- In-cluster: one `relay.py` pod, one Gunicorn worker, one replica.
- External: `server.py`, desktop Tauri compute nodes, Macs, Windows PCs, Raspberry Pi GPU/AI hat
  nodes, and other compute nodes.
- No in-cluster backend/GPU service is required for this phase.

The relay is technically stateful today because server registrations, client messages, and replies
are stored in process memory. With the current one-pod/one-worker model, state loss on pod death is
accepted for now. Redis (or equivalent shared state), multi-worker, and multi-replica relay
architecture are future work and out of scope for this onboarding guide.

API v1 relay-blind E2EE guardrails remain mandatory:

- relay-owned state/logs/diagnostics must remain ciphertext-only (plus safe routing metadata)
- no plaintext relay payload logging or storage
- fail closed if E2EE envelope expectations are not met

## Canonical deployment artifacts (token.place-owned)

- Relay image: `ghcr.io/futuroptimist/tokenplace-relay`
- Relay chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`

Tag policy:

- Preferred for staging/prod validation: immutable `main-<shortsha>`
- Convenience only: mutable `main-latest` (not production sign-off)

## Sugarkube-owned values and command wrappers

The chart/image are published by token.place. Environment values files, approved version pins, and
`just` wrappers are owned by sugarkube.

Run deployment commands from a **sugarkube checkout**, not from token.place.

Staging first install pattern:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Staging upgrade pattern:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Production uses the same approved immutable tag and swaps the env values file:

- `docs/examples/tokenplace.values.prod.yaml`

Sugarkube-specific token.place wrappers are expected to exist after subsequent sugarkube prompts.

## Hostnames and routing

Default hostnames:

- Staging: `https://staging.token.place`
- Production: `https://token.place`

Operators may override hostnames in sugarkube values and Cloudflare route configuration.

## Validation

After install/upgrade:

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

For production validation, replace the host with `https://token.place`.

## Runbooks

- [k3s-sugarkube-dev.md](k3s-sugarkube-dev.md)
- [k3s-sugarkube-staging.md](k3s-sugarkube-staging.md)
- [k3s-sugarkube-prod.md](k3s-sugarkube-prod.md)
