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
> Use Sugarkube-owned dev inputs that exist in your checkout.
>
> Required before running commands:
> - `<DEV_VALUES_FILE>` (dev values file path)
> - either `<VERSION_FILE>` or an explicit `version=`/`tag=` argument

First install with a version file:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=<DEV_VALUES_FILE> version_file=<VERSION_FILE> default_tag=main-REPLACE_SHORTSHA
```

First install with an explicit tag:

```bash
just helm-oci-install release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=<DEV_VALUES_FILE> tag=main-REPLACE_SHORTSHA default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release with a version file:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=<DEV_VALUES_FILE> version_file=<VERSION_FILE> default_tag=main-REPLACE_SHORTSHA
```

Upgrade existing release with an explicit tag:

```bash
just helm-oci-upgrade release=tokenplace namespace=tokenplace chart=oci://ghcr.io/futuroptimist/charts/tokenplace values=<DEV_VALUES_FILE> tag=main-REPLACE_SHORTSHA default_tag=main-REPLACE_SHORTSHA
```

## Validation checklist

```bash
kubectl -n tokenplace get deploy,po,svc,ingress
kubectl -n tokenplace rollout status deploy/tokenplace --timeout=180s
curl -fsS https://<DEV_HOST>/livez
curl -fsS https://<DEV_HOST>/healthz
curl -fsS https://<DEV_HOST>/
```

Use the actual dev ingress host for `<DEV_HOST>` (do not validate against staging).

Optional note: true relay traffic validation requires a registered external compute node plus an
E2EE client-flow probe; health/root checks alone do not prove register/poll/request/response flow.

## Rollback

- Record baseline revision: `helm history tokenplace -n tokenplace`
- Roll back release and/or tag per Sugarkube process.
- Re-run validation checks and capture operator notes.

## Notes

- Avoid stale local-chart/legacy-contract workflows deployment.
- Redis/shared-state/multi-replica relay architecture remains future work and out of scope.
