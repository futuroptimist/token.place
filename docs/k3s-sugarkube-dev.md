# token.place relay on k3s+sugarkube (dev)

> **Environment status:** secondary/non-target environment for this prompt.

## Scope

Keep dev aligned with the same relay-only contract used for staging/prod:

- In-cluster: `relay.py` only.
- External compute remains out-of-cluster: `server.py`, desktop Tauri nodes, Macs, Windows PCs,
  Raspberry Pi GPU/AI hat nodes, and other compute hosts.
- No required in-cluster backend/GPU service.
- Preserve API v1 relay-blind E2EE guardrails.

## Artifacts and tags

- Image: `ghcr.io/futuroptimist/tokenplace-relay`
- Chart: `oci://ghcr.io/futuroptimist/charts/tokenplace`
- Prefer immutable `main-<shortsha>` when validating changes; `main-latest` is convenience-only.

## Deployment commands (run from Sugarkube repo)

> These commands run from a **Sugarkube checkout**, not from token.place.
>
> `docs/examples/tokenplace.values.dev.yaml` and `docs/apps/tokenplace.version` are
> **Sugarkube-owned future contract artifacts** expected after follow-up Sugarkube prompts land.

First install:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=docs/examples/tokenplace.values.dev.yaml version_file=docs/apps/tokenplace.version default_tag=main-REPLACE_SHORTSHA
```

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://staging.token.place/livez
curl -fsS https://staging.token.place/healthz
curl -fsS https://staging.token.place/
```

Optional note: true relay traffic validation requires a registered external compute node plus an
E2EE client-flow probe; health/root checks alone do not prove register/poll/request/response flow.

## Rollback

- Record baseline revision: `helm history tokenplace -n tokenplace`
- Roll back release and/or tag per Sugarkube process.
- Re-run validation checks and capture operator notes.

## Notes

- Avoid stale local-chart/legacy-contract workflows (`./deploy/charts/tokenplace-relay`).
- Redis/shared-state/multi-replica relay architecture remains future work and out of scope.
