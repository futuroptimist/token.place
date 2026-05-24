# token.place relay on k3s+sugarkube (staging)

> **Environment status:** **Current + planned hardening**.
> Staging validates relay-only operations before production rollout.

## Scope

Relay-only staging at `https://staging.token.place` by default.

- In-cluster runtime is `relay.py` only.
- `server.py` and desktop/hardware compute nodes remain external.
- No in-cluster backend/GPU service is required in this phase.

The relay is technically stateful in this phase (in-memory registrations/messages/replies), so
current staging target is one pod, one Gunicorn worker, one replica. Pod-loss state loss is
accepted for now.

## Artifacts and tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Preferred tag: immutable `main-<shortsha>`
- Convenience only: `main-latest` (not for sign-off)

## Deployment workflow (from sugarkube repo)

Run from a **sugarkube checkout**, not from token.place.

First install pattern:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Upgrade pattern for existing release:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml,docs/examples/tokenplace.values.staging.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Sugarkube-specific tokenplace wrappers may be added later; they should preserve the same OCI
chart source and immutable-tag promotion process.

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

## Rollback

- Record revision before rollout: `helm history tokenplace -n tokenplace`
- Roll back to prior known-good release revision and immutable image tag.
- Re-run validation checks above after rollback.

## Operator notes

- Default staging hostname is `https://staging.token.place`; operators may override hostname via
  sugarkube values and Cloudflare route configuration.
- Redis/multi-replica relay state architecture is future work and out of scope for this phase.
