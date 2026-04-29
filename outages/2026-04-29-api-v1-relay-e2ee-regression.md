# Outage: API v1 distributed relay E2EE regression

- **Date:** 2026-04-29
- **Slug:** `api-v1-relay-e2ee-regression`
- **Affected area:** Distributed relay bridge for `/api/v1/chat/completions`
- **Introduction point:** PR #813, merge commit `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`

## Summary
A regression introduced in PR #813 changed the distributed API v1 relay bridge so relay-owned state
handled plaintext OpenAI-style `messages` during distributed inference. This violated token.place's
relay-blind E2EE invariant (relay should only route ciphertext plus minimal routing metadata).

The issue was later contained by fail-closing the unsafe path (Prompt 1), followed by E2EE restoration
for distributed API v1 relay traffic (Prompt 2), and then permanent anti-regression safeguards
(Prompt 3).

## Severity / impact
- **Severity:** High (security invariant regression in distributed inference path).
- **Confirmed impact:** Code-level plaintext exposure risk existed in relay-owned queue/forwarding
  path for API v1 distributed requests.
- **Not confirmed:** No confirmed real-world exploitation or confirmed compromise is documented in
  repository evidence at this time.

## Core invariant violated
token.place's core invariant for distributed client/server inference is: **the relay only sees
ciphertext payloads and routing metadata, not plaintext model content**.

Legacy `/sink` + `/faucet` flow was designed around encrypted envelope fields (for example
`chat_history`, `cipherkey`, `iv`, and `client_public_key`) so relay infrastructure can route work
without reading model payload content.

## What happened
PR #813 (`Route relay API v1 chat through registered desktop compute node`) introduced an API v1
bridge across relay endpoints and queue semantics:
- `/relay/api/v1/chat/completions`
- `/relay/api/v1/source`
- `api_v1_request` queue payload shape
- compute-node handling for `api_v1_request.messages`
- distributed API v1 provider posting raw OpenAI-style `messages` through relay path

That bridge unintentionally routed plaintext `messages` through relay-owned state, breaking the
relay-blind E2EE contract.

## Timeline
- **2026-04-?? (introduction):** PR #813 merged as
  `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`, introducing API v1 distributed relay plaintext
  handling regression.
- **Attempted partial mitigation:** PR #831 proposed registration-token hardening to reduce
  unauthenticated plaintext dispatch exposure, but did not restore relay-blind E2EE. It was
  closed/not merged in favor of full containment + restoration + safeguards.
- **Prompt 1 containment:** PR #834 merged as `86c27f9` (with follow-up containment commits),
  fail-closing unsafe distributed API v1 relay dispatch and surfacing safe error behavior
  (`distributed_api_v1_relay_disabled`) instead of forwarding plaintext.
- **Prompt 2 restoration:** PR #836 merged as `a0f9a6c`, restoring distributed API v1 relay
  functionality through relay-blind E2EE envelope handling.
- **Prompt 3 prevention:** PR #842 merged as `5151be7`, adding permanent multi-layer safeguards to
  prevent plaintext relay regressions.

## Root cause
- Architectural drift during API v1 bridge refactor: API v1 request compatibility was prioritized,
  but the relay-blind E2EE envelope contract was not preserved end-to-end.
- Missing invariants at multiple layers: static checks, runtime sentinels, egress/log guards, and
  contract tests were insufficient at the time of introduction.

## Technical details
- Regression introduced relay queue payload semantics (`api_v1_request`) carrying plaintext
  OpenAI-style `messages`.
- Compute-node distributed handling consumed `api_v1_request.messages`, creating a plaintext
  distributed path through relay-owned state.
- This differed from legacy encrypted relay flow where relay sees encrypted fields and cannot read
  user/model content.

## Affected code paths
- Distributed API v1 relay bridge path centered on:
  - `POST /relay/api/v1/chat/completions`
  - `POST /relay/api/v1/source`
  - queue payload type `api_v1_request`
  - distributed provider logic posting OpenAI-style `messages` through relay

## Unaffected code paths
- Legacy encrypted `/sink` + `/faucet` model envelope pattern as designed (ciphertext + routing
  metadata) remained the reference invariant.
- Non-distributed/local inference paths were not the direct subject of this outage record.

## Detection
Detection came from E2EE invariant review and follow-on hardening work that identified distributed
API v1 relay plaintext handling as a contract violation. Subsequent containment/restoration work and
Prompt 3 safeguards codified the violation conditions and prevention checks.

## Immediate containment
Prompt 1 containment (PR #834, merge commit `86c27f9`) disabled unsafe distributed API v1 relay
plaintext dispatch and forced fail-closed behavior. User-visible behavior temporarily moved to the
safe error path rather than attempting insecure distributed execution.

## Restoration or deferment
Prompt 2 restoration (PR #836, merge commit `a0f9a6c`) restored distributed API v1 relay behavior
through relay-blind E2EE envelope routing. The final user-visible state after Prompt 2 is that
successful distributed API relay is available again via E2EE-preserving flow.

(If future audits find environments where Prompt 2 restoration was intentionally deferred, update
this section with explicit fail-closed deferment details for that branch/release.)

## Permanent remediation / regression prevention
Prompt 3 safeguards (PR #842, merge commit `5151be7`) added multi-layer anti-regression coverage:
- static forbidden-pattern checks,
- runtime relay-state sentinels,
- network egress sentinels,
- log/diagnostics sentinels,
- local-vs-distributed contract tests,
- and agent-facing docs/instructions reinforcing relay-blind E2EE requirements.

## Verification performed
- Incident documentation authored under `outages/` with schema sidecar.
- Repository checks (see command list in this change) confirm outage entry references key
  identifiers (`PR #813`, `PR #831`, `0f01d38...`, `api_v1_request`, `relay-blind E2EE`,
  `distributed_api_v1_relay_disabled`).

## Follow-up items
- Keep this outage entry linked in future relay/API v1 architecture reviews.
- Require explicit relay-blind E2EE contract sign-off for any new distributed protocol bridge.
- Continue to treat any plaintext relay-owned state path as fail-closed by default unless replaced
  by reviewed E2EE envelope design.
