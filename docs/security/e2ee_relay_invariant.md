# Relay-blind E2EE invariant

Distributed inference over relay paths must be relay-blind end-to-end encryption.

## Core rules
- Relay-visible data must be ciphertext plus safe routing metadata only.
- Never queue plaintext model content in relay-owned state (`client_inference_requests`,
  response queues, or stream session state).
- Never send OpenAI-style plaintext (`messages`, `prompt`, tool arguments, model output text)
  to relay endpoints.
- Never log, diagnose, or echo distributed plaintext payloads.

## For coding agents
- Do not route `messages`/`prompt` through relay in plaintext.
- Do not add relay queue fields containing plaintext model content.
- Do not POST raw model payloads to relay endpoints.
- Do not include plaintext distributed payload content in logs/diagnostics/errors.
- If plaintext is required, run outside relay-blind distributed mode or fail closed.
- Add/update sentinel tests for relay state, network egress, logs, diagnostics, and API responses.

## Guardrails in tests
- `tests/unit/test_e2ee_relay_invariant.py` contains static and runtime anti-regression sentinels.
