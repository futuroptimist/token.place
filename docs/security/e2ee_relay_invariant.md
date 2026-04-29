# Relay-blind E2EE invariant

Distributed relay inference must remain relay-blind end-to-end encrypted (E2EE).

## Rules

- Relay-visible request/response payloads must be ciphertext plus safe routing metadata only.
- Never route OpenAI-style `messages`, legacy `prompt`, tool args, or model output plaintext through relay queues.
- Never queue plaintext in `client_inference_requests` or relay-owned response/streaming state.
- Never POST raw model payloads to relay endpoints.
- Never log, diagnose, or echo distributed plaintext payloads.
- If plaintext handling is required for a feature, run it outside distributed relay mode or fail closed.

## For coding agents

Before editing relay/API/compute-node bridge code:

1. Add/update sentinel tests for relay state, outbound network calls, logs, diagnostics, and API responses.
2. Ensure distributed mode either:
   - uses an approved encrypted envelope, or
   - fails closed before queue/network dispatch.
3. Preserve local plaintext behavior for non-distributed API routes without allowing relay plaintext paths.

Sentinel strings used by regression tests:

- `E2EE_SENTINEL_SHOULD_NEVER_REACH_RELAY_PLAINTEXT`
- `E2EE_SENTINEL_SHOULD_NEVER_LEAVE_PROCESS_AS_PLAINTEXT`
- `E2EE_SENTINEL_SHOULD_NEVER_APPEAR_IN_LOGS_OR_DIAGNOSTICS`
