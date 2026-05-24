# token.place relay on k3s+sugarkube (staging)

> **Environment status:** **Current + planned hardening**.
> Staging validates relay operations before production rollout.

## Scope

Relay-only staging with default hostname `https://staging.token.place`.

- In scope for sugarkube: `relay.py` only.
- Out of scope/in external compute plane: `server.py`, desktop Tauri compute nodes, Macs,
  Windows PCs, Raspberry Pi GPU/AI hats, and other compute nodes.
- No in-cluster backend/GPU service is required for this phase.

## Operational model

The relay is currently stateful because registrations/messages/replies live in process memory.

- one pod
- one Gunicorn worker
- one replica
- state loss on pod death is accepted for now

Redis/shared memory stores and multi-replica relay architecture are future work.

## Artifact references

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Preferred staging tag: immutable `main-<shortsha>`
- `main-latest` is convenience-only and is not staging sign-off material.

## Deployment workflow (from sugarkube checkout)

Run from a **sugarkube** checkout (not token.place):

First install:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Sugarkube-specific token.place wrapper recipes are expected as follow-up convenience commands.

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

Operators may override hostname/routing in sugarkube values and Cloudflare tunnel/route config.

## Rollback

- Record revision before rollout: `helm history tokenplace -n tokenplace`
- Roll back to previous known-good revision/tag.
- Re-run validation checks after rollback.
