# token.place relay on k3s+sugarkube (dev)

> **Environment status:** **Planned / active development target**.
> Focus is relay-only deployment; compute nodes stay external in this phase.

## Scope

Deploy `relay.py` to sugarkube dev for iterative validation.

- In-cluster: relay deployment only.
- External: `server.py` and desktop compute nodes.
- No in-cluster backend/GPU service required.

## Deployment workflow (from sugarkube checkout)

Use OCI chart/image flows from a **sugarkube** checkout:

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

Dev hostname/routing can be overridden in sugarkube values and Cloudflare tunnel settings.
