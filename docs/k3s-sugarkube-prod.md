# token.place relay on k3s+sugarkube (prod)

> **Environment status:** **Planned / post-staging promotion target**.
> Production runbook is prepared now for consistent relay operations.

## Scope

Production deploys `relay.py` only with default hostname `https://token.place`.

- In scope for sugarkube: `relay.py`.
- External compute plane: `server.py`, desktop Tauri compute nodes, Macs, Windows PCs,
  Raspberry Pi GPU/AI hats, and other compute nodes.
- No in-cluster backend/GPU service is required in this phase.

## Operational model

Current relay runtime characteristics:

- one pod
- one Gunicorn worker
- one replica
- in-memory state for registrations/messages/replies
- accepted state loss on pod death until shared state architecture exists

Redis/shared state and multi-replica relay are future work.

## Artifact references

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Production sign-off tag: immutable `main-<shortsha>`
- `main-latest` is convenience-only and not approved for production sign-off.

## Deployment workflow (from sugarkube checkout)

Run from a **sugarkube** checkout (not token.place):

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.prod.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

For existing releases, use upgrade:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.prod.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Sugarkube-specific token.place wrappers are expected as follow-up convenience commands.

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/
```

Operators may override hostname/routing in sugarkube values and Cloudflare tunnel/route config.

## Rollback

- Record revision before rollout: `helm history tokenplace -n tokenplace`
- Roll back to previous known-good revision/tag.
- Re-run validation checks after rollback.
