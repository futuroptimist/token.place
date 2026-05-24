# token.place relay on k3s+sugarkube (staging)

> **Environment status:** current validation environment before production promotion.

## Scope

Staging runs **relay only** at `https://staging.token.place` by default.

- In-cluster: `relay.py` only (one pod, one worker, one replica)
- External: `server.py` and all compute nodes (desktop Tauri, Macs, Windows PCs, Raspberry Pi GPU
  / AI hats, and other nodes)
- No required in-cluster backend or GPU service in this phase

The relay is currently stateful in-memory (registrations/messages/replies). State loss on pod death
is accepted for now.

## Artifacts and tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Preferred tag for sign-off: immutable `main-<shortsha>`
- `main-latest` is convenience only and should not be used for production sign-off.

## Deployment workflow (from sugarkube checkout)

Run these commands from a sugarkube repo checkout (not token.place):

First install:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Sugarkube-specific token.place wrappers may be added as follow-up ergonomics.

## Hostname

Default staging hostname is `https://staging.token.place`.
Operators may override this in sugarkube values and Cloudflare routes.

## Validation

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

## Rollback

- Inspect release history first: `helm history tokenplace -n tokenplace`
- Roll back to previous known-good revision/tag
- Re-run validation commands above
- Record incident notes if user-visible impact occurred

## Guardrails

- Keep API v1 relay-blind E2EE invariants intact.
- Do not treat legacy relay routes as active production-path requirements.
