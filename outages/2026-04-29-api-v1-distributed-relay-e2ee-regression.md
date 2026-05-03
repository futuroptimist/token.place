# Outage: API v1 distributed relay E2EE regression

- **Date:** 2026-04-29
- **Slug:** `api-v1-distributed-relay-e2ee-regression`
- **Severity:** High (security invariant violation)
- **Introduction point:** PR #813, merge commit `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`
- **Containment:** PR #834 (merge commit `86c27f9`)
- **Restoration:** PR #836 (merge commit `a0f9a6c`)
- **Permanent safeguards:** PR #842 (merge commit `5151be7`)

## Summary
A regression introduced by PR #813 accidentally routed plaintext OpenAI-style `messages`
through relay-owned distributed API v1 state. This violated token.place’s relay-blind E2EE
invariant for distributed inference (relay should only handle ciphertext plus minimal routing
metadata).

## Severity / impact
- **Impact class:** security/design invariant breach in distributed API v1 path.
- **Confirmed risk:** relay-owned queue/state could contain plaintext model payloads for affected
  distributed API v1 requests.
- **Confirmed exploitation:** none in repository evidence at time of incident writeup.
- **User-visible effect during containment:** distributed API v1 relay execution was forced to a
  safe fail-closed path until compliant routing was restored.

## Core invariant violated
For distributed inference, the relay must be **relay-blind**: it may see ciphertext and routing
metadata only, never plaintext model prompts/messages.

Legacy `/sink` + `/faucet` behavior was designed around this invariant by forwarding encrypted
payload fields (for example `chat_history`, `cipherkey`, `iv`, and `client_public_key`) rather than
plaintext chat content.

## What happened
PR #813 (`Route relay API v1 chat through registered desktop compute node`) introduced a new API v1
bridge path that included:
- `/relay/api/v1/chat/completions`
- `/relay/api/v1/source`
- `api_v1_request` relay state
- compute-node handling of `api_v1_request.messages`

The distributed API v1 provider then POSTed raw OpenAI-style `messages` through relay-managed state,
creating an accidental plaintext bridge in a path that must remain E2EE.

## Timeline
- **Introduction:** PR #813 merged at commit
  `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`.
- **Partial mitigation attempt:** PR #831 proposed requiring registration tokens, reducing
  unauthenticated-node plaintext dispatch risk. It did **not** restore relay-blind E2EE semantics
  and was closed/not merged as incomplete remediation.
- **Prompt 1 containment:** PR #834 merged (`86c27f9`), disabling unsafe plaintext distributed API
  v1 relay dispatch and enforcing fail-closed behavior (`distributed_api_v1_relay_disabled`).
- **Prompt 2 restoration:** PR #836 merged (`a0f9a6c`), restoring distributed API v1 relay through
  an E2EE envelope path aligned with relay-blind requirements.
- **Prompt 3 prevention:** PR #842 merged (`5151be7`), adding permanent anti-regression safeguards
  across static checks, runtime sentinels, network/log guards, and contract tests.

## Root cause
- Contract drift during API v1 bridge refactor: endpoint/routing changes were implemented without
  preserving the pre-existing ciphertext-only relay contract.
- Missing comprehensive invariant coverage at merge time for “no plaintext payload in relay-owned
  distributed state” across newly added API v1 relay structures.

## Technical details
The regression path introduced relay-owned structures that could carry OpenAI-style `messages` in
plaintext (`api_v1_request.messages`) for distributed processing. This differs from the legacy
ciphertext envelope approach where relay persistence/queueing uses encrypted fields and keys.

PR #831 improved who could register/dispatch but did not change the payload confidentiality model;
therefore it reduced one threat dimension (unauthenticated dispatch) without restoring E2EE
correctness.

## Affected code paths
- Distributed API v1 relay bridge path introduced in PR #813:
  - `/relay/api/v1/chat/completions`
  - `/relay/api/v1/source`
  - relay-owned `api_v1_request` state carrying `messages`
- Distributed API v1 provider flow that POSTed raw `messages` through relay-owned request state.

## Unaffected code paths
- Legacy relay E2EE pattern (`/sink` + `/faucet`) designed around encrypted payload transport.
- Non-distributed/local execution paths that do not route plaintext model payloads through
  relay-owned distributed request queues.

## Detection
Detection occurred through E2EE invariant review and follow-up security hardening work that compared
legacy ciphertext contracts against API v1 distributed bridge behavior and identified plaintext relay
state usage as non-compliant.

## Immediate containment
Prompt 1 (PR #834) fail-closed the unsafe distributed API v1 relay dispatch. User-visible behavior
was intentionally shifted to a safe error path when distributed API v1 relay execution depended on
the non-compliant plaintext bridge.

## Restoration or deferment
Prompt 2 resolution is **restoration** in this repository state: PR #836 restored distributed API
v1 relay processing through a relay-blind E2EE envelope path. Distributed operation resumed only
with compliant encrypted relay transport.

(If future backports diverge, document explicit fail-closed deferment rather than re-enabling any
plaintext relay bridge.)

## Permanent remediation / regression prevention
Prompt 3 (PR #842) introduced layered safeguards:
- static forbidden-pattern checks,
- runtime relay-state plaintext sentinels,
- network egress sentinels,
- log/diagnostics sentinels,
- local-vs-distributed contract tests,
- and agent-facing invariant documentation/instructions.

These controls are intended to block reintroduction of plaintext distributed relay payload handling.

## Verification performed
- Reviewed merge history for introduction/containment/restoration/safeguard sequence:
  - PR #813 introduction commit `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`
  - PR #834 containment merge `86c27f9`
  - PR #836 restoration merge `a0f9a6c`
  - PR #842 safeguards merge `5151be7`
- Confirmed this outage entry is docs/metadata only (no production runtime changes).

## Follow-up items
- Maintain invariant wording in agent/developer docs and outage records to keep relay-blind E2EE
  requirements explicit during future API evolution.
- Require reviewer sign-off on ciphertext-only distributed relay ownership whenever bridge contracts
  are modified.
- Keep PR #831 referenced as partial mitigation history, not as full remediation.
