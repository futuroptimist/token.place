# Outage: API v1 distributed relay E2EE regression

- **Date:** 2026-04-29
- **Slug:** `api-v1-distributed-relay-e2ee-regression`
- **Affected area:** distributed relay bridge for API v1 chat/completions

## Summary
An API v1 distributed relay bridge change introduced in **PR #813** (merge commit
`0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`) unintentionally routed plaintext OpenAI-style
`messages` through relay-owned request state. This violated token.place's relay-blind E2EE
invariant for distributed inference. The issue was later contained by failing closed on unsafe
plaintext relay dispatch, then remediated by restoring distributed API v1 relay execution through
an E2EE envelope path, followed by permanent anti-regression safeguards.

## Severity / impact
- **Severity:** High (security invariant regression in distributed relay path).
- **Confirmed impact:** Code-level exposure risk existed because plaintext model payloads could
  traverse relay-owned distributed queue/state in the affected path.
- **Not confirmed:** No confirmed exploitation or observed real-world compromise based on
  repository evidence at incident documentation time.

## Core invariant violated
For distributed client/server inference, the relay must be **relay-blind E2EE**:
- relay sees ciphertext payloads and routing metadata only;
- relay does not queue, log, forward, or diagnose plaintext model payload content.

Legacy relay `/sink` + `/faucet` flow was designed around encrypted fields such as
`chat_history`, `cipherkey`, `iv`, and `client_public_key` so relay-owned state remained
ciphertext-only.

## What happened
PR #813 introduced API v1 distributed bridge components (`/relay/api/v1/chat/completions`,
`/relay/api/v1/source`, `api_v1_request`, and compute-node handling of
`api_v1_request.messages`) and changed the distributed API v1 provider to POST raw
OpenAI-style `messages` via relay-owned pathing. That shape created a plaintext distributed
relay path incompatible with the core E2EE invariant.

A later proposed mitigation in **PR #831** required registration tokens and reduced
unauthenticated-node exposure, but it did not restore relay-blind E2EE for payload content.
PR #831 was therefore closed/not merged in favor of a stricter sequence:
1. Prompt 1 containment (fail closed),
2. Prompt 2 restoration/deferment decision,
3. Prompt 3 permanent safeguards,
4. this Prompt 4 incident documentation.

## Timeline
- **2026-04-XX:** Regression introduced by **PR #813**.
  - Merge commit: `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`
  - Commit subject: `Merge pull request #813 from futuroptimist/codex/refactor-chat-path-to-use-api-v1-tw00iw`
- **2026-04-XX:** **PR #831** proposed partial mitigation (registration-token tightening) and was
  later closed because it did not restore relay-blind E2EE.
- **2026-04-XX:** Prompt 1 containment merged in **PR #834** (merge commit `86c27f9`), temporarily
  disabling unsafe distributed API v1 relay dispatch and forcing fail-closed behavior.
- **2026-04-XX:** Prompt 2 restoration merged in **PR #836** (merge commit `a0f9a6c`), restoring
  distributed API relay behavior through an E2EE envelope path.
- **2026-04-XX:** Prompt 3 safeguards merged in **PR #842** (merge commit `5151be7`), adding
  multi-layer anti-regression protections.
- **2026-04-29:** Prompt 4 outage entry added.

## Root cause
Root cause was contract drift during API v1 relay bridge refactor:
- new distributed API v1 relay wiring optimized request routing semantics;
- E2EE boundary assumptions from legacy encrypted relay flows were not fully carried into the new
  API v1 distributed bridge;
- review and test coverage at introduction time did not fully block plaintext relay-owned state for
  all distributed API v1 variants.

## Technical details
Regression shape introduced with PR #813:
- added `/relay/api/v1/chat/completions` relay entrypoint,
- added `/relay/api/v1/source` relay polling/source endpoint,
- added `api_v1_request` relay/bridge queue object,
- added compute-node handling for `api_v1_request.messages`,
- changed distributed API v1 provider to POST raw OpenAI-style `messages` through relay path.

This differed from legacy encrypted relay contracts that passed ciphertext envelope fields
(`chat_history`, `cipherkey`, `iv`, `client_public_key`) through relay-owned state.

## Affected code paths
- Distributed API v1 relay bridge path added/refactored in PR #813.
- Relay-owned queue/state shape carrying `api_v1_request.messages` plaintext content.
- Compute-node distributed handling for API v1 relay-polled requests derived from that state.

## Unaffected code paths
- Local (non-distributed) inference paths that do not route model payloads through relay-owned
  distributed queue/state.
- Legacy encrypted relay flows designed around ciphertext fields when used as intended.

## Detection
Issue was detected through code-path review and follow-up test hardening during relay/API v1
stabilization work, specifically by identifying that distributed API v1 bridge payload handling had
deviated from relay-blind E2EE requirements.

## Immediate containment
Prompt 1 emergency containment (PR #834, merge `86c27f9`) made unsafe distributed API v1 relay
plaintext dispatch fail closed.

User-visible temporary behavior during containment:
- distributed API v1 relay mode returned safe error behavior instead of executing unsafe plaintext
  relay dispatch;
- landing-page and related distributed path behavior reflected the explicit safe failure mode
  while containment was active.

## Restoration or deferment
Prompt 2 outcome in this repository state:
- **Restoration completed** via **PR #836** (merge `a0f9a6c`) by routing distributed API v1 relay
  inference through a relay-blind E2EE envelope path.
- This replaced temporary fail-closed unavailability from Prompt 1 for the restored flow.

If reviewing historical intermediate states, containment-only behavior from Prompt 1 was an
intentional temporary safety posture before Prompt 2 restoration landed.

## Permanent remediation / regression prevention
Prompt 3 safeguards merged in PR #842 (merge `5151be7`) added multi-layer protections:
- static forbidden-pattern tests for relay plaintext payload regressions,
- runtime relay-state sentinels,
- network egress sentinels,
- log/diagnostic sentinels,
- local-vs-distributed contract tests,
- agent-facing docs/instructions reinforcing relay-blind E2EE requirements.

## Verification performed
- Confirmed introduction point and remediation sequence from git history:
  - PR #813 introduction (`0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`)
  - Prompt 1 containment PR #834
  - Prompt 2 restoration PR #836
  - Prompt 3 safeguards PR #842
- Verified outage documentation includes required facts and terms:
  - `relay-blind E2EE`
  - `api_v1_request`
  - `PR #831` partial mitigation context
  - containment fail-closed behavior and restoration state

## Follow-up items
- Continue requiring relay-blind E2EE checks for any new distributed transport path.
- Keep Prompt 3 sentinel/test/doc guardrails mandatory in CI/review.
- Preserve this outage record as canonical context for future relay/API v1 design and security
  reviews.
