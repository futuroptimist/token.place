# API v1 desktop bridge cold-runtime timeout (2026-05-21)

## Summary
The API v1 encrypted desktop relay path could return browser-side 504 timeouts on the
first request because the desktop compute-node bridge advertised readiness before the
llama.cpp runtime was actually initialized.

## Impact
- Browser `POST /api/v1/chat/completions` could fail with `compute_node_timeout` (504).
- Relay repeatedly polled `/api/v1/relay/responses/retrieve` and observed 404 until timeout.
- Desktop bridge had accepted and decrypted work but had not submitted
  `/api/v1/relay/responses` before relay timeout.

## Timeline
1. Browser sends `POST /api/v1/chat/completions`.
2. Relay accepts queue via `/api/v1/relay/requests`.
3. Desktop bridge receives and decrypts API v1 E2EE work.
4. Relay repeatedly checks `/api/v1/relay/responses/retrieve` and receives 404.
5. Relay returns final 504 `compute_node_timeout` to browser request.

## Root cause
`compute_node_bridge.py` called `ComputeNodeRuntime.ensure_model_ready()`, which validates
model availability/download only. Actual runtime initialization occurred later when request
processing triggered `model_manager.get_llm_instance()`. That cold init cost happened inside
the active relay timeout budget.

## Why existing tests missed it
Existing encrypted desktop bridge E2E coverage uses fake/instant desktop runtime behavior.
That validates envelope flow and encryption semantics, but not startup readiness semantics for
cold llama.cpp initialization.

## Fix
- Added `ComputeNodeRuntime.ensure_api_v1_runtime_ready()` to:
  - run existing model-readiness checks,
  - force runtime warmup via `model_manager.get_llm_instance()`,
  - fail closed if warmup returns `None`,
  - fail closed unless runtime exposes callable non-streaming
    `create_chat_completion`.
- Updated desktop bridge startup to require API v1 runtime warmup before emitting `started`
  and before entering register/poll loop.
- On warmup failure, bridge now emits structured `type: "error"` and exits nonzero.

## Regression tests added
- Unit tests for runtime prewarm helper success/failure cases.
- Unit tests for bridge startup ordering and fail-closed behavior when warmup fails.

## Manual verification (Windows/local relay)
1. Start relay with distributed provider enabled.
2. Start desktop Tauri compute node.
3. Confirm bridge logs model init start/ready before first registration/poll event.
4. Send a chat message from the UI.
5. Confirm relay logs `/api/v1/relay/responses` 200 before `/api/v1/chat/completions`
   returns.
6. Confirm no 504 and no repeated retrieve-only 404 loop.
