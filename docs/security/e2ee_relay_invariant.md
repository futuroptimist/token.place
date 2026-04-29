# relay-blind E2EE invariant

Distributed inference through relay paths must be relay-blind end-to-end encryption.

## Rules

- Relay-visible distributed payloads must be ciphertext-only plus safe routing metadata.
- Never put OpenAI-style plaintext `messages`, legacy `prompt`, tool-call arguments, or model output text in relay-owned queue state (`client_inference_requests`) or relay diagnostics/logging.
- Never POST raw model payloads to relay-dispatch endpoints; distributed API relay paths must fail closed unless an approved encrypted envelope is used.
- If a feature requires plaintext processing, run it in local/non-distributed mode or fail closed.

## Safeguard tests

- `tests/unit/test_e2ee_relay_invariant.py` includes static forbidden-pattern checks and plaintext sentinel checks for relay state, network egress, logs/diagnostics, and local-vs-distributed API contracts.
- `tests/test_relay.py` and `tests/unit/test_api_v1_compute_provider.py` cover relay queue contracts and encrypted envelope behavior.

## For coding agents

Before changing relay/API code:

1. Do not route `messages`/`prompt` plaintext through relay.
2. Do not add relay queue fields containing plaintext model content.
3. Do not send raw model payloads to relay endpoints.
4. Do not log/diagnose/echo distributed plaintext payload content.
5. If uncertain, add sentinel checks across relay state, outbound requests, logs, diagnostics, and API responses.
