# Outage: API v1 distributed relay E2EE regression

- **Date:** 2026-04-29
- **Slug:** `api-v1-distributed-relay-e2ee-regression`
- **Affected area:** distributed API v1 relay bridge (`/relay/api/v1/chat/completions` and `/relay/api/v1/source`)

## Summary
A regression introduced in PR #813 (merge commit `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`) changed distributed API v1 relay flow so raw OpenAI-style `messages` were routed through relay-owned state (`api_v1_request`) instead of an approved relay-blind E2EE envelope path. This violated token.place's core relay invariant.

Containment, restoration/deferment, and permanent safeguards were then shipped in a three-prompt response sequence:
- Prompt 1 containment: PR #834 (`86c27f9`) fail-closed unsafe distributed API v1 relay dispatch.
- Prompt 2 restoration: PR #836 (`a0f9a6c`) restored distributed API v1 relay via relay-blind E2EE envelope routing.
- Prompt 3 safeguards: PR #842 (`5151be7`) added multi-layer anti-regression protections.

No confirmed real-world exploitation is recorded in repository evidence for this incident at time of writing.

## Severity / impact
- **Severity:** High (security/invariant regression in distributed relay path).
- **Confirmed code-level risk:** relay-owned queue/state could carry plaintext model payload content for distributed API v1 requests.
- **User-visible impact during containment:** distributed API v1 relay mode was temporarily fail-closed (`distributed_api_v1_relay_disabled`) and landing-page behavior returned safe error messaging instead of unsafe distributed dispatch.
- **Confirmed exploitation status:** none confirmed from repository evidence/log review attached to this incident.

## Core invariant violated
For distributed client/server inference, the relay must be blind to model payload plaintext and only handle ciphertext plus minimal routing metadata.

Legacy `/sink` + `/faucet` flows were designed around this invariant by forwarding encrypted blobs/fields (for example `chat_history`, `cipherkey`, `iv`, and `client_public_key`) rather than raw prompt text.

The API v1 distributed bridge regression broke that invariant by persisting and forwarding plaintext `messages` in relay-owned request state.

## What happened
PR #813 introduced a new API v1 relay bridge and compute-node request handling intended to route landing-page/API v1 traffic through registered desktop compute nodes. In that implementation, the distributed API provider posted raw OpenAI-style `messages` into relay-managed request objects (`api_v1_request`) and compute-node handling consumed `api_v1_request.messages`.

That implementation improved routing coverage but unintentionally reintroduced relay visibility into plaintext payload content, conflicting with the relay-blind E2EE requirement.

## Timeline
- **Introduction:** PR #813 merged at `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed` (`Merge pull request #813 ... refactor-chat-path-to-use-api-v1...`).
- **Partial mitigation proposal:** PR #831 proposed registration-token requirements to reduce unauthenticated-node plaintext exposure; closed/not merged because it did not restore relay-blind E2EE.
- **Prompt 1 containment:** PR #834 merged at `86c27f9`; unsafe distributed API v1 relay dispatch fail-closed and user path moved to safe error behavior.
- **Prompt 2 restoration/deferment:** PR #836 merged at `a0f9a6c`; distributed API v1 relay restored via relay-blind E2EE envelope path (final state after Prompt 2: distributed mode available again through E2EE).
- **Prompt 3 safeguards:** PR #842 merged at `5151be7`; layered static/runtime/test/docs safeguards added to prevent similar regressions.

## Root cause
1. **Contract drift during API v1 bridge refactor:** migration to API v1 relay endpoints focused on connectivity/dispatch success but did not preserve the strict ciphertext-only relay contract from legacy sink/faucet.
2. **Insufficient invariant-specific tests at introduction time:** pre-existing checks did not block PR #813-style plaintext relay-state patterns.
3. **Security scope mismatch in early mitigation:** PR #831 reduced unauthenticated exposure but treated node authentication as sufficient mitigation, which is not equivalent to relay blindness.

## Technical details
Regression shape introduced by PR #813:
- Added `/relay/api/v1/chat/completions`.
- Added `/relay/api/v1/source`.
- Added `api_v1_request` relay-owned request shape.
- Added compute-node handling for `api_v1_request.messages`.
- Updated distributed API v1 provider behavior to POST raw OpenAI-style `messages` through relay-managed state.

Security implication:
- Relay-owned memory/queue/diagnostic paths had potential access to plaintext model payload data for distributed API v1 requests, violating the E2EE design boundary.

## Affected code paths
- Distributed API v1 relay bridge path added in PR #813 using `/relay/api/v1/chat/completions` + `/relay/api/v1/source` with `api_v1_request.messages` in relay-owned state.

## Unaffected code paths
- Legacy encrypted relay contract (`/sink` + `/faucet`) that forwards encrypted fields (`chat_history`, `cipherkey`, `iv`, `client_public_key`) rather than plaintext payload text.
- Non-distributed/local execution paths that do not route through relay-owned distributed queue state.

## Detection
Detection came from security/invariant review identifying that distributed API v1 relay bridging introduced plaintext `messages` handling in relay-owned state, contrary to the relay-blind E2EE invariant.

Prompt 3 later codified this detection class into automated checks (static forbidden-pattern scans and runtime/network/log sentinels) so future violations fail in CI rather than review.

## Immediate containment
Prompt 1 containment (PR #834, `86c27f9`) fail-closed the unsafe distributed API v1 relay dispatch path.

Behavioral effect:
- Distributed API v1 relay execution was intentionally disabled while unsafe plaintext handling existed.
- User-visible landing/API behavior shifted to safe error handling (`distributed_api_v1_relay_disabled`) instead of allowing insecure distributed dispatch.

## Restoration or deferment
Prompt 2 outcome for this incident: **restoration completed**.

PR #836 (`a0f9a6c`) restored distributed API v1 relay operation using a relay-blind E2EE envelope path so relay-managed state no longer requires plaintext OpenAI-style `messages` payload content.

(If future backports/cherry-picks diverge from this branch state, update this outage entry with explicit deferment notes.)

## Permanent remediation / regression prevention
Prompt 3 (PR #842, `5151be7`) delivered defense-in-depth safeguards across the stack:
- Static forbidden-pattern checks for PR #813-style plaintext relay patterns.
- Runtime relay-state sentinels.
- Network egress sentinels.
- Log/diagnostic plaintext sentinels.
- Local-vs-distributed contract tests.
- Agent-facing docs/instructions reinforcing relay-blind E2EE requirements.

Together these controls reduce likelihood of reintroducing plaintext distributed relay handling and improve detection speed if drift occurs.

## Verification performed
- Verified outage record includes required introduction reference to PR #813 and merge commit `0f01d38b01e7e6bab44bc42eeaab9eccd3a5e0ed`.
- Verified explicit treatment of PR #831 as partial mitigation only (closed/not merged).
- Verified documentation of Prompt 1 containment fail-closed user-visible state.
- Verified documentation of Prompt 2 final state (restored through relay-blind E2EE).
- Verified documentation of Prompt 3 safeguards and prevention layers.
- Verified wording does not assert confirmed exploitation without evidence.

## Follow-up items
- Continue requiring Prompt 3 safeguard suite in CI and pre-commit workflows.
- During future relay/API migrations, require explicit “ciphertext-only relay-state” design checklist signoff before merge.
- Keep this outage linked in relay E2EE design/review templates so future contributors understand the regression class and required controls.
