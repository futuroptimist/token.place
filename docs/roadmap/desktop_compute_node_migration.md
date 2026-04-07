# token.place desktop compute-node migration roadmap (canonical)

> **Status:** Canonical implementation sequence for prompt 0 and implementation prompts 1-7.
> **Audience:** maintainers implementing desktop parity, relay operations, and post-parity API v1
> distributed compute migration.

## Purpose

This document is the source of truth for the migration order from today's `server.py`-centric
runtime toward a desktop-first compute-node architecture. It is intentionally explicit about what is
**current state**, **near-term**, and **target state** so we do not overstate existing
implementation.

- Tauri design context: [../design/tauri_desktop_client.md](../design/tauri_desktop_client.md)
- Top-level project context: [../../README.md](../../README.md)
- Relay onboarding: [../relay_sugarkube_onboarding.md](../relay_sugarkube_onboarding.md)

## Current state (as of this roadmap)

- `server.py` is the production compute-node runtime today.
- `desktop-tauri/` exists as an MVP and is not yet at `server.py` parity.
- `relay.py` already supports the legacy sink/source contract and multi-node registration.
- API v1 endpoints exist for current integrations, but API v1 **distributed compute** migration is a
  later phase, after desktop parity.

## Why this order matters

The sequence below prevents architectural drift and avoids breaking existing consumers:

1. We first converge runtime behavior (`server.py` + desktop) before changing network contracts.
2. We move lightweight relay operations to sugarkube early because relay has lower operational risk
   than moving compute.
3. We defer API v1 distributed-compute migration until parity is proven, so protocol migration is not
   mixed with runtime parity work.

## 7-step implementation sequence (exact order)

1. **Prompt 1: Shared compute-node runtime skeleton**
   - Define the shared runtime surface used by both `server.py` and `desktop-tauri/`.
2. **Prompt 2: `server.py` integration onto shared runtime (no behavior regression)**
   - Keep legacy sink/source and current API behavior stable.
3. **Prompt 3: `desktop-tauri/` runtime integration**
   - Move desktop from prompt-testing MVP toward real compute-node execution using shared runtime.
4. **Prompt 4: Desktop model-management parity layer**
   - Implement browse/download/select flows that map to runtime-usable GGUF artifacts.
5. **Prompt 5: Legacy multi-node relay hardening + readiness checks**
   - Keep legacy relay contract; improve multi-node reliability and operational confidence.
6. **Prompt 6: Relay-on-sugarkube rollout (dev → staging → prod)**
   - Relay is deployed as lightweight cluster service while compute nodes remain external.
7. **Prompt 7: Post-parity API v1 distributed-compute migration planning/implementation start**
   - Begin migration only after parity and relay operations exit criteria are satisfied.

## What "desktop parity" means (concrete definition)

`desktop-tauri/` parity with `server.py` means all of the following are true:

- Shared compute-node runtime powers equivalent inference lifecycle semantics in both paths.
- Equivalent model selection semantics exist for runtime-supported models.
- Equivalent streaming and cancellation behavior is available.
- Equivalent relay registration and encrypted forwarding behavior is available on legacy contract.
- Equivalent operator-observable health diagnostics exist for runtime-critical states.

Parity does **not** require API v1 distributed compute to be complete.

## Phase exit criteria

### Step 1 exit criteria

- Shared runtime interface documented and committed.
- Contract tests exist for runtime lifecycle primitives.

### Step 2 exit criteria

- `server.py` runs via shared runtime with no known regression in legacy behavior.
- Existing integration tests for server + relay continue passing.

### Step 3 exit criteria

- `desktop-tauri/` executes real inference jobs through shared runtime.
- Desktop flow is no longer only a local prompt tester path.

### Step 4 exit criteria

- Desktop model management shows canonical model family and concrete GGUF artifact metadata.
- Downloaded artifact is directly consumable by runtime.

### Step 5 exit criteria

- Multi-node legacy relay readiness checklist passes in controlled environments.
- Registration/failover behavior is documented and reproducible.

### Step 6 exit criteria

- Relay deployment runbooks (dev/staging/prod) are validated by operators.
- Relay is stably hosted on sugarkube for target environments without moving compute into cluster.

### Step 7 exit criteria

- API v1 distributed compute migration checklist is green.
- Security, auth, and compatibility plans are approved before cutover.

## Acceptance checklists

### Desktop parity readiness

- [ ] Shared runtime integrated in both `server.py` and `desktop-tauri/`.
- [ ] Streaming + cancellation parity verified.
- [ ] Model-management parity requirements met.
- [ ] Desktop relay forwarding validated on legacy contract.

### Legacy multi-node relay readiness

- [ ] Multiple nodes can register and serve traffic on sink/source contract.
- [ ] Failover behavior exercised and documented.
- [ ] Operator diagnostics for registration and health are available.

### Relay-on-sugarkube readiness

- [ ] Dev/staging/prod runbooks reviewed and environment owners assigned.
- [ ] Ingress/tunnel path validated for relay endpoints.
- [ ] Rollback path documented and tested at least once.

### API v1 distributed migration readiness

- [ ] Desktop parity checklist complete.
- [ ] Relay-on-sugarkube checklist complete.
- [ ] API v1 auth/routing/data-contract migration plan reviewed.
- [ ] Cutover and rollback criteria approved.

## Out of scope for this roadmap phase

- Claiming `desktop-tauri/` already replaces `server.py` in production.
- Claiming distributed API v1 compute is already implemented.
- Collapsing relay and compute migrations into one unvalidated cutover.
