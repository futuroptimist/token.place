# Desktop compute-node migration roadmap (canonical)

This is the canonical migration plan for moving token.place from today's
`server.py`-centered runtime toward a desktop-led compute-node architecture.

- **Current state:** desktop-tauri is an MVP and is **not** feature-parity with `server.py`.
- **Near-term target:** achieve desktop parity on the legacy relay sink/source contract first.
- **Future target:** migrate distributed compute to API v1 **after** parity is complete.
- **Deployment guardrail (short-to-medium term):** sugarkube/k3s runs `relay.py` only; compute
  nodes (`server.py`, desktop-tauri) run on operator workstations.

See also:

- [README.md](../../README.md)
- [Tauri desktop design](../design/tauri_desktop_client.md)
- [Architecture](../ARCHITECTURE.md)
- [Relay on sugarkube onboarding](../relay_sugarkube_onboarding.md)

## Why the sequence matters

The order is designed to reduce migration risk:

1. Preserve production behavior while extracting a shared runtime.
2. Prove desktop can run as a real compute node on today's contract.
3. Move lightweight relay operations to sugarkube early.
4. Delay protocol migration (API v1 distributed compute) until parity and operations are stable.

This avoids changing compute runtime, deployment topology, and network contract all at once.

## 7-step implementation sequence (prompts 1–7)

### Prompt 1 — Shared compute-node runtime extraction

Extract compute-node concerns from `server.py` into a shared runtime usable by both
`server.py` and desktop-tauri.

**Exit criteria**

- Shared runtime boundary defined and implemented.
- `server.py` continues to pass existing tests using shared runtime.
- Desktop-tauri can invoke the same runtime abstraction (even if incomplete).

### Prompt 2 — Desktop parity on legacy contract

Promote desktop-tauri from MVP/local prompt tester to a real compute node that can
participate on the legacy relay contract.

**Exit criteria**

- Desktop handles inference lifecycle, streaming, and cancellation via shared runtime.
- Desktop participates in relay flow with legacy sink/source semantics.
- Desktop parity checklist (below) is satisfied.

### Prompt 3 — Legacy multi-node relay hardening

Use existing relay multi-node registration and forwarding to support mixed compute-node
fleets (`server.py` and desktop nodes) on the legacy contract.

**Exit criteria**

- Stable multi-node registration observed.
- Failover/load-balancing behavior validated on legacy contract.
- Operational runbooks updated for mixed-node operation.

### Prompt 4 — Relay-on-sugarkube rollout (legacy contract)

Deploy `relay.py` to sugarkube as lightweight control-plane infrastructure while compute
nodes remain external.

**Exit criteria**

- Dev/staging/prod relay runbooks exist and are validated.
- Health checks, ingress, and rollback procedures documented.
- Relay-on-sugarkube readiness checklist (below) is satisfied.

### Prompt 5 — Model-management parity completion

Complete desktop model-management parity requirements so desktop compute nodes match
`server.py` operational expectations.

**Exit criteria**

- Canonical model-family references are surfaced in desktop flows.
- GGUF artifact selection/state is explicit and operator-visible.
- Model browse + download flows are implemented with clear status/error handling.

### Prompt 6 — Post-parity API v1 distributed migration

After parity and stable operations, migrate distributed compute from legacy sink/source
assumptions toward API v1-aligned distributed contracts.

**Exit criteria**

- Migration plan and compatibility strategy approved.
- API v1 distributed compute path validated in staging.
- Legacy contract deprecation plan documented.

### Prompt 7 — Legacy pathway retirement and steady-state ops

Retire legacy-only pathways once API v1 distributed compute is production ready.

**Exit criteria**

- Desktop-led compute node path is default for forward development.
- Legacy-only pathways are either removed or explicitly sunset.
- Post-migration operations/runbooks are complete.

## Feature parity definition (desktop-tauri vs server.py)

Desktop is considered parity-ready only when all of the following are true:

- Runs the same shared compute-node runtime behaviors as `server.py` for inference lifecycle.
- Supports equivalent streaming and cancellation semantics.
- Uses token.place-compatible encryption envelope handling in relay flows.
- Participates in legacy relay sink/source registration + polling without special casing.
- Supports model-management parity requirements in desktop UX:
  - canonical Meta model-family reference (for example: <https://www.llama.com/models/>)
  - explicit GGUF artifact selection used by runtime
  - model browse + download flow
  - default relay URL `https://token.place` with user override support
- Keeps compute-mode contract aligned (`auto`, `cpu`, `metal`, `cuda`) across workstation
  runtimes and desktop/operator surfaces.
- Reflects operator target platforms:
  - Windows 11 + NVIDIA CUDA (`cuda`)
  - macOS Apple Silicon + Metal (`metal`)
  - CPU fallback for unsupported hosts (`cpu`)
  - Raspberry Pi as a later low-power workstation target (not part of relay-on-sugarkube rollout)

## Readiness checklists

### Desktop parity readiness

- [ ] Shared compute-node runtime is consumed by both `server.py` and desktop-tauri.
- [ ] Desktop executes real inference workloads (not fake sidecar only).
- [ ] Streaming and cancellation match server runtime behavior.
- [ ] Relay integration passes legacy contract integration tests.
- [ ] Model-management parity requirements are implemented.
- [x] Compute-mode normalization contract is shared/tested (`auto`/`cpu`/`metal`/`cuda`).

### Legacy multi-node relay readiness

- [ ] Multiple compute nodes can register simultaneously.
- [ ] Relay routing/failover works under node churn.
- [ ] Node auth/registration controls are validated.
- [ ] Operator runbooks include mixed-fleet troubleshooting.

### Relay-on-sugarkube readiness

- [ ] `relay.py` chart/runtime config is environment-specific (dev/staging/prod).
- [ ] Health, ingress, and tunnel checks are automated or documented.
- [ ] Rollback procedure is tested for relay image/chart changes.
- [ ] Compute-node operators understand relay stays lightweight in-cluster.

### API v1 distributed migration readiness

- [ ] Desktop/server parity has been accepted.
- [ ] Legacy contract dependencies are inventoried.
- [ ] API v1 distributed contract compatibility layer is tested.
- [ ] Decommission plan for legacy pathways is reviewed.

## Current vs target summary

- **Current:** `relay.py` + `server.py` legacy flow is the production baseline; desktop-tauri is MVP.
  `server/server_app.py` remains compatibility-only and should not diverge from `server.py`.
- **Near-term:** desktop and `server.py` co-evolve through a shared runtime while relay moves onto
  sugarkube. Compute nodes continue to run on end-user/operator workstations.
- **Target:** post-parity, post-API-v1 distributed compute with secure API v1-aligned components.
