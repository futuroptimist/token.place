# Relay-blind E2EE invariant (distributed inference)

## Core rule

For distributed inference, relay-visible traffic is **ciphertext only** plus safe routing metadata.
Relay-owned systems must never receive or expose plaintext model payloads.

Forbidden plaintext examples:
- OpenAI-style `messages`
- legacy `prompt`
- assistant response text
- tool-call arguments/content
- equivalent future model payload fields

## Guardrails in tests

`tests/unit/test_e2ee_relay_invariant.py` enforces anti-regression safeguards:
- static forbidden source-pattern scanning for PR #813-style relay plaintext paths;
- runtime relay-state sentinel checks (queue/session/diagnostics);
- outbound network egress sentinel checks (`requests.post/get/request` and
  `requests.Session.request`);
- log + diagnostics sentinel checks.

Contract expectations retained:
- Local/non-distributed API paths may process plaintext locally.
- Distributed relay API v1 plaintext path must stay fail-closed unless using approved E2EE envelope.
- Legacy `/sink` + `/faucet` flow remains ciphertext-envelope-based.

## For coding agents

- Never route OpenAI `messages` or legacy `prompt` through relay in plaintext.
- Never add relay queue fields containing plaintext model content.
- Never POST raw model payloads to relay endpoints.
- Never log, diagnose, or echo distributed plaintext payload content.
- If a feature requires plaintext, keep it outside relay-blind distributed mode or fail closed.
- When in doubt, add sentinel tests for relay state, outbound network calls, logs,
  diagnostics, and API responses.
