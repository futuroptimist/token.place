# token.place relay on k3s+sugarkube (dev)

> **Environment status:** secondary/non-target environment for this prompt.

## Scope

Keep dev aligned with the same relay-only contract used for staging/prod:

- In-cluster: `relay.py` only.
- External compute remains out-of-cluster: `server.py`, desktop Tauri nodes, Macs, Windows PCs,
  Raspberry Pi GPU/AI hat nodes, and other compute hosts.
- No required in-cluster backend/GPU service.
- Preserve API v1 relay-blind E2EE guardrails.

## Notes

- Avoid stale local-chart/legacy-contract workflows (`./deploy/charts/tokenplace-relay`).
- Dev runbooks/wrappers are Sugarkube-owned and may evolve independently from this repo.
- Redis/shared-state/multi-replica relay architecture remains future work and out of scope.
