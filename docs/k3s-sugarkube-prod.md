# token.place relay on k3s+sugarkube (prod)

> **Environment status:** **Planned / post-staging promotion target**.
> Production runbook for relay-only deployment.

## Scope

Production runs `relay.py` only in sugarkube.

- `server.py` plus desktop/hardware compute nodes remain external.
- No in-cluster backend/GPU service is required in this phase.
- Relay state is in-memory today (registrations/messages/replies), so production uses one pod,
  one Gunicorn worker, one replica.
- State loss on pod death is accepted for now.

Redis-backed/multi-replica relay state is future work and out of scope.

## Artifacts and tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Required for sign-off: immutable `main-<shortsha>` approved in staging
- Convenience only: mutable `main-latest`

## Deployment workflow (from sugarkube repo)

Run from a **sugarkube checkout**, not from token.place.

Production install/upgrade pattern:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.prod.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

If release does not exist yet, use `just helm-oci-install` with the same values/version/tag
inputs.

Sugarkube-specific tokenplace wrappers may be introduced later; they should preserve the same OCI
chart source and immutable tag policy.

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://token.place/livez
curl -fsS https://token.place/healthz
curl -fsS https://token.place/
```

## Rollback

- Record revision before rollout: `helm history tokenplace -n tokenplace`
- Roll back immediately to prior known-good revision/tag if validation fails.
- Re-run validation commands post-rollback.

## Operator notes

- Default production hostname is `https://token.place`; operators may override hostname in
  sugarkube values and Cloudflare routes.
- Keep API v1 relay-blind E2EE guardrails intact; do not treat legacy relay routes as active
  production paths.
