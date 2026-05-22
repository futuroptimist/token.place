# API v1 Desktop Bridge Cold Runtime Timeout (2026-05-21)

## Summary
The API v1 encrypted desktop relay path could return `504 compute_node_timeout` when the first
chat request hit a desktop compute node that had registered before its llama.cpp runtime was fully
initialized.

## Impact
- Browser calls to `POST /api/v1/chat/completions` could time out after ~30 seconds.
- Relay selected a registered compute node and queued encrypted API v1 work, but no completion
  response was posted back in time.
- Desktop bridge logs showed E2EE payload receipt/decryption without a matching
  `response_submitted` event before relay timeout.

## Timeline (from supplied logs)
1. Browser submits `POST /api/v1/chat/completions`.
2. Relay accepts work via `POST /api/v1/relay/requests`.
3. Desktop bridge receives/decrypts API v1 E2EE work.
4. Relay repeatedly polls `POST /api/v1/relay/responses/retrieve` and receives `404`.
5. Relay returns `504` with `compute_node_timeout`.

## Root cause
Bridge startup treated model download/verification as ready state. Real runtime initialization
occurred lazily on first request via `model_manager.get_llm_instance()`, so cold llama.cpp init
plus generation consumed the distributed timeout budget.

## Why existing tests missed it
Existing desktop E2EE integration tests used fast fake runtimes that validated encryption/protocol
behavior, but did not enforce startup readiness semantics tied to real runtime warmup ordering.

## Fix
- Added `ComputeNodeRuntime.ensure_api_v1_runtime_ready()` to:
  - ensure model availability,
  - force runtime creation through `get_llm_instance()`,
  - require callable non-streaming `create_chat_completion`,
  - update runtime diagnostics (`runtime_ready`, `runtime_ready_error`).
- Updated desktop bridge startup to call warmup before emitting `started` and before first
  register/poll cycle.
- Warmup failure now emits a startup `error`, returns non-zero, and fails closed without
  registering/polling for relay work.

## Regression tests added
- Unit coverage for runtime warmup helper success/failure conditions.
- Unit coverage asserting bridge warmup occurs before first poll.
- Unit coverage asserting warmup failures prevent polling and emit startup errors.

## Manual verification (Windows/local relay)
1. Start relay with distributed compute provider configured.
2. Start desktop Tauri compute node bridge.
3. Verify bridge logs show runtime warmup completion before first register/poll.
4. Send chat message from UI.
5. Verify relay logs `POST /api/v1/relay/responses` `200` before
   `/api/v1/chat/completions` returns.
6. Confirm no `504 compute_node_timeout` and no repeated retrieve-only `404` loop.
