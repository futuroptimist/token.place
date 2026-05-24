# token.place relay on k3s+sugarkube (prod)

> **Environment status:** production runbook for controlled relay-only rollout.

## Scope

Production sugarkube deploys `relay.py` only.

- In-cluster: one pod / one Gunicorn worker / one replica
- External: `server.py`, desktop Tauri compute nodes, Macs, Windows PCs, Raspberry Pi GPU/AI hat
  nodes, and other compute nodes
- No required in-cluster backend/GPU service in this phase

The relay keeps operational state in memory (registrations/messages/replies), so state loss on pod
restart/death is accepted in the current model.

## Artifacts and tag policy

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Use the same staging-approved immutable tag: `main-<shortsha>`
- `main-latest` is mutable convenience and is not valid for production sign-off

## Deployment workflow (from sugarkube checkout)

Run from sugarkube checkout (not token.place).

Install (first-time):

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.prod.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Upgrade (existing release):

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.prod.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Sugarkube-specific token.place wrappers may be added in follow-up prompts.

## Hostname

Default production hostname is `https://token.place`.
Operators may override hostnames in sugarkube values and Cloudflare routes.

## Validation

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/
```

## Rollback

- Inspect release history: `helm history tokenplace -n tokenplace`
- Roll back to previous known-good revision/tag
- Re-run validation commands
- Capture deployment outcome and follow-ups

## Guardrails

- Preserve API v1 relay-blind E2EE invariants.
- Do not assume legacy relay routes are the active production runtime path.
