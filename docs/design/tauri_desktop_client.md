# token.place Tauri desktop client design

## Status snapshot (April 2026)

- `desktop-tauri/` is an **MVP**, not parity with `server.py`.
- The current fake/placeholder sidecar path is **transitional only**.
- The near-term objective is to turn desktop-tauri into a **real compute node** on the
  existing relay contract.
- API v1 distributed-compute migration is explicitly **post-parity**.

Canonical migration sequence: [docs/roadmap/desktop_compute_node_migration.md](../roadmap/desktop_compute_node_migration.md).

## Strategy

Tauri is the forward-looking desktop path for token.place. The desktop app should evolve from a
local prompt tester into a production-grade compute node that can co-exist with `server.py` during
migration.

### Core strategy constraints

1. `server.py` and desktop-tauri must co-evolve in lockstep through a **shared compute-node runtime**.
2. Desktop parity on the **legacy relay sink/source contract** comes before API v1 distributed migration.
3. Relay operations can move to sugarkube earlier because `relay.py` is lightweight and stateless.

## Current vs near-term vs target

### Current state

- `server.py` is the primary compute-node implementation.
- `relay.py` supports legacy sink/source contract and multi-node registration.
- desktop-tauri provides MVP scaffolding but does not yet replace `server.py`.

### Near-term state (post-parity goal)

- Desktop-tauri runs shared compute-node runtime behaviors equivalent to `server.py`.
- Desktop nodes and `server.py` can both operate on legacy relay flows.
- `relay.py` is deployed on sugarkube for dev/staging/prod operations.

### Target state (later)

- Distributed compute contracts are migrated to API v1 alignment.
- Legacy-only pathways are retired after validated migration.

## Architecture direction

```text
Desktop UI (Tauri)
  -> shared compute-node runtime
  -> model runtime (real sidecar)
  -> local encryption
  -> relay.py (legacy contract during parity phase)
  -> downstream compute/network flow
```

The fake sidecar remains acceptable only as a temporary development seam while real sidecar/runtime
parity is implemented.

## Model-management parity requirements

Desktop parity includes model-management behaviors, not just prompt UI.

- Present a canonical Meta model-family reference:
  - <https://www.llama.com/models/>
- Show the actual GGUF artifact the runtime is using (path/name/version where available).
- Support model browse + download UX (with explicit status/progress/error handling).
- Default relay URL should be `https://token.place`, but must remain overridable for local/staging.

## Operational alignment

Relay operational target is sugarkube deployment of `relay.py` while compute nodes remain external
through the parity phases.

- Onboarding guide: [docs/relay_sugarkube_onboarding.md](../relay_sugarkube_onboarding.md)
- Environment runbooks:
  - [docs/k3s-sugarkube-dev.md](../k3s-sugarkube-dev.md)
  - [docs/k3s-sugarkube-staging.md](../k3s-sugarkube-staging.md)
  - [docs/k3s-sugarkube-prod.md](../k3s-sugarkube-prod.md)

## Out of scope for MVP/parity phases

- Claiming desktop-tauri already replaces `server.py`.
- Claiming API v1 distributed compute is already implemented.
- Coupling parity work to simultaneous protocol migration.

## Exit signal for this design

This design is considered fulfilled when roadmap parity gates are met and desktop-tauri is accepted
as a real compute node implementation on the shared runtime and legacy relay contract.
