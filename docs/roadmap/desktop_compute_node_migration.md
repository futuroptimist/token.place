# Desktop compute-node migration roadmap (canonical)

> Canonical plan for prompts 1-7.
>
> This document is the source of truth for migration order, parity definitions,
> and exit criteria. If other docs disagree, update them to match this one.

## Why this sequence exists

The order protects current users while we migrate from `server.py`-centric compute to the
`desktop-tauri/` path:

1. `desktop-tauri/` must first become a **real compute node** (not only a local prompt tester).
2. `server.py` and desktop must then co-evolve through a **shared compute-node runtime contract**.
3. Only **after parity** do we migrate distributed compute onto API v1 semantics.

This prevents two risky failures:

- Migrating protocol first, then discovering desktop cannot do existing compute-node work.
- Splitting behavior between `server.py` and desktop and losing compatibility.

## Current state vs target state

### Current state (April 2026)

- `server.py` is the reference compute node for production-style workloads.
- `relay.py` supports legacy sink/source behavior and multi-node registration mechanics.
- `desktop-tauri/` is MVP status: useful foundation, not full `server.py` parity yet.
- API v1 exists for client-facing API compatibility, but distributed compute migration to API v1
  is **not complete**.

### Near-term target

- Relay runs reliably on sugarkube.
- Compute nodes still run outside sugarkube.
- Desktop and `server.py` share a compute-node runtime contract and pass parity checks.

### End target (post-parity, post-API-v1 migration)

- Desktop is the forward-looking replacement path for `server.py` compute-node duties.
- Distributed compute paths use API v1-aligned contracts.
- Relay + compute components are securely aligned around API v1 behavior.

## The 7-step implementation sequence (must stay in this order)

1. **Shared compute-node runtime contract and fixture harness**
   - Define the common runtime interface used by both `server.py` and `desktop-tauri/`.
   - Add compatibility fixtures to keep behavior lockstep.
2. **Desktop compute-node execution parity (core inference paths)**
   - Ensure desktop can execute the same request/response lifecycle expected from current nodes.
3. **Desktop model-management parity**
   - Implement browse + download + selected-model visibility with explicit GGUF artifact tracking.
4. **Desktop relay integration parity (legacy contract)**
   - Desktop registers and participates as a legacy relay compute node under existing sink/source
     semantics.
5. **Legacy multi-node hardening and observability**
   - Validate mixed fleets (`server.py` + desktop) under relay failover and multi-node operation.
6. **Relay-on-sugarkube operational rollout**
   - Move relay operations to sugarkube runbooks/dev->staging->prod while compute remains external.
7. **Post-parity API v1 distributed compute migration**
   - Migrate node-to-relay distributed compute contracts to API v1-aligned semantics.

## Feature parity definition: desktop vs `server.py`

Desktop is considered parity-ready only when all items below are true:

- Same compute-node runtime contract inputs/outputs as `server.py` for agreed legacy flows.
- Equivalent streaming/cancellation semantics for the shared runtime harness.
- Equivalent relay registration and sink/source lifecycle behavior (legacy mode).
- Equivalent model selection/loading guarantees for supported local models.
- Equivalent error categories surfaced to callers/operators.
- Equivalent security posture for plaintext handling and encryption boundaries.

## Exit criteria by step

### Step 1 exit criteria

- Runtime interface documented and versioned.
- Contract tests run against both `server.py` and desktop runtime adapters.

### Step 2 exit criteria

- Desktop passes core inference fixtures used for `server.py` reference behavior.
- No known blocker where desktop requires server-only logic for basic compute execution.

### Step 3 exit criteria

- Desktop UI/runtime can browse model family metadata and download selected GGUF artifacts.
- The exact runtime GGUF artifact in use is visible and auditable.

### Step 4 exit criteria

- Desktop can register and handle work via existing relay legacy node semantics.
- Relay interoperability tests pass with desktop-only and mixed-node topologies.

### Step 5 exit criteria

- Multi-node reliability checks pass with failover, restart, and registration churn.
- Operator-facing logs/metrics are sufficient to debug mixed-fleet issues.

### Step 6 exit criteria

- Relay dev/staging/prod runbooks are validated for sugarkube deployment.
- Rollback and health validation procedures are documented and exercised.

### Step 7 exit criteria

- Distributed compute paths are API v1 aligned end-to-end.
- Legacy-only distributed compute pathways are retired or explicitly gated.

## Acceptance checklists

### Desktop parity readiness

- [ ] Shared runtime contract implemented in desktop and `server.py` adapters.
- [ ] Core inference contract tests pass on both runtimes.
- [ ] Streaming and cancellation semantics match accepted baseline.
- [ ] Model management parity requirements are met (browse/download/artifact visibility).

### Legacy multi-node relay readiness

- [ ] Desktop can register as a compute node with current legacy relay contracts.
- [ ] Mixed fleet (`server.py` + desktop) tested under failover.
- [ ] Registration churn/recovery behavior documented.

### Relay-on-sugarkube readiness

- [ ] Relay health/readiness checks defined and verified.
- [ ] Config + secret expectations documented for operators.
- [ ] Dev/staging/prod release + rollback procedures documented.

### API v1 distributed migration readiness

- [ ] Desktop/server shared runtime parity sustained through migration tests.
- [ ] API v1 distributed contract documented and validated.
- [ ] Rollback path from API v1 distributed mode to legacy mode is documented.

## Related docs

- Top-level orientation and status notes: [README.md](../../README.md)
- Desktop strategy/design details: [../design/tauri_desktop_client.md](../design/tauri_desktop_client.md)
- Relay sugarkube onboarding: [../relay_sugarkube_onboarding.md](../relay_sugarkube_onboarding.md)
- Environment runbooks:
  - [../k3s-sugarkube-dev.md](../k3s-sugarkube-dev.md)
  - [../k3s-sugarkube-staging.md](../k3s-sugarkube-staging.md)
  - [../k3s-sugarkube-prod.md](../k3s-sugarkube-prod.md)
