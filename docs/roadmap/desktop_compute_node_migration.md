# token.place desktop compute-node migration roadmap (canonical)

> Status: canonical sequencing doc for prompt 0. This is the source of truth for
> migration order, parity definition, and phase exit criteria.

## Why this order exists

token.place currently has production-proven behavior centered on `server.py` plus the
legacy relay `/sink` + `/source` contract. The desktop-tauri app is the forward path,
but today it is still MVP-level. The migration therefore prioritizes correctness and
operational continuity:

1. First reach desktop parity with `server.py` behavior using shared runtime semantics.
2. Then move deployment topology toward relay-first sugarkube operations.
3. Only after parity and relay operational maturity, migrate distributed compute onto
   API v1-aligned contracts.

Reordering these steps would raise risk by changing protocol and runtime at the same
time, making regressions harder to isolate.

## Current, near-term, target states

- **Current state**
  - `server.py` is the mature compute-node implementation.
  - `desktop-tauri/` is an MVP shell and not yet parity.
  - `relay.py` already supports legacy `/sink` + `/source` flows and multi-node
    registration.
- **Near-term state (pre-API-v1 migration)**
  - Desktop and server co-evolve in lockstep via shared compute-node runtime behavior.
  - Relay can be moved onto sugarkube as a lightweight front door while compute nodes
    remain external.
- **Target state (post-parity, post-API-v1 migration)**
  - Distributed compute nodes are aligned on API v1 contracts and secure operational
    controls.
  - `server.py` legacy-only responsibilities can be reduced/retired per later prompts.

## 7-step implementation sequence (must remain in this order)

1. **Lock scope + parity contract**
   - Define compute-node runtime contract shared by `server.py` and desktop-tauri.
   - Freeze parity requirements and acceptance tests.
2. **Runtime extraction for lockstep evolution**
   - Refactor shared compute-node behavior behind reusable interfaces/modules.
   - Keep `server.py` as reference implementation while desktop adopts the same core
     runtime semantics.
3. **Desktop compute-node integration (legacy relay contract)**
   - Upgrade desktop-tauri from local tester to real compute node over legacy relay
     `/sink` + `/source` registration/serving.
4. **Desktop/server parity hardening**
   - Close parity gaps (streaming, cancellation, model loading, encryption behavior,
     error semantics, observability and redaction).
5. **Relay operationalization on sugarkube**
   - Make relay deployment/runbooks/dev-staging-prod flow operationally repeatable.
   - Continue using external compute nodes through legacy contract.
6. **API v1 distributed-compute migration (post-parity)**
   - Introduce API v1-aligned distributed compute contracts and phased node migration.
   - Preserve compatibility bridges during cutover.
7. **Post-migration simplification + legacy retirement**
   - Remove obsolete compatibility paths once usage is drained.
   - Finalize docs, runbooks, and operator defaults around the new model.

## Feature parity definition: desktop-tauri vs `server.py`

Desktop parity means the desktop compute-node path can stand in for `server.py` for
agreed node responsibilities, with equivalent behavior in these areas:

- **Node lifecycle:** register, poll, process, and respond through relay legacy
  contract.
- **Inference behavior:** model load/unload semantics, streaming, cancellation,
  backpressure, and stable error classes.
- **Crypto and transport behavior:** envelope compatibility, no plaintext leakage to
  relay, and consistent key handling semantics.
- **Model management UX:** browse + download + explicit GGUF artifact selection, with
  clear provenance links.
- **Operational observability:** health checks, redacted logs, version identifiers,
  diagnosable failure states.

Parity does **not** require immediate API v1 distributed compute migration.

## Exit criteria by phase

### Step 1 exit criteria
- Shared compute-node contract documented and approved.
- Parity test matrix defined and linked from implementation docs.

### Step 2 exit criteria
- Shared runtime module(s) are consumed by `server.py` without behavior regressions.
- Regression suite passes for existing `server.py` flows.

### Step 3 exit criteria
- Desktop-tauri can register as a relay node and execute real workloads via legacy
  relay flow.
- Fake/mock sidecar usage is clearly transitional and gated.

### Step 4 exit criteria
- Parity checklist is green (see checklist section below).
- Known behavior diffs are either closed or explicitly accepted with owner/date.

### Step 5 exit criteria
- Dev/staging/prod relay-on-sugarkube runbooks exist and are executable.
- Operators can roll forward/back relay without changing compute-node runtime.

### Step 6 exit criteria
- API v1 distributed-compute contract is documented, tested, and staged.
- Migration includes rollback and compatibility bridge verification.

### Step 7 exit criteria
- Legacy-only paths are removed after usage drain-down evidence.
- Docs and operator playbooks reflect only supported target architecture.

## Acceptance checklists

### Desktop parity readiness
- [ ] Desktop registers/serves workloads through relay legacy flow.
- [ ] Streaming + cancellation behavior matches `server.py` semantics.
- [ ] Crypto envelope compatibility validated against existing clients.
- [ ] Model browse/download/select flow implemented with explicit artifact display.
- [ ] Redacted diagnostics and health signals available for operators.

### Legacy multi-node relay readiness
- [ ] Relay can register and route across multiple external nodes.
- [ ] Token/auth policy for node registration is documented and enforced.
- [ ] Failure handling for node drain/unavailable states is validated.

### Relay-on-sugarkube readiness
- [ ] Relay image/version pinning strategy documented for envs.
- [ ] Ingress/tunnel/routing path validated for target hostname.
- [ ] Health checks and rollback procedures practiced at least once per env.

### API v1 distributed migration readiness
- [ ] Desktop/server parity checklist is complete.
- [ ] Legacy-to-v1 compatibility bridge exists and is tested.
- [ ] Security controls for inter-component API v1 communication are documented.
- [ ] Operator runbooks include staged cutover + rollback paths.

## Related docs

- Top-level orientation: [README.md](../../README.md)
- Desktop strategy and architecture: [../design/tauri_desktop_client.md](../design/tauri_desktop_client.md)
- Relay sugarkube onboarding: [../relay_sugarkube_onboarding.md](../relay_sugarkube_onboarding.md)
- Environment runbooks:
  - [../k3s-sugarkube-dev.md](../k3s-sugarkube-dev.md)
  - [../k3s-sugarkube-staging.md](../k3s-sugarkube-staging.md)
  - [../k3s-sugarkube-prod.md](../k3s-sugarkube-prod.md)
