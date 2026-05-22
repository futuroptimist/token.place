# API v1 desktop bridge cold-runtime timeout (2026-05-21)

## Summary
The API v1 encrypted desktop relay flow could return `504 compute_node_timeout` on first
request because the desktop bridge advertised readiness before llama.cpp runtime warmup
completed.

## Impact
- Browser POST to `/api/v1/chat/completions` timed out after the distributed provider window.
- Relay selected a compute node and queued encrypted work, but the desktop node did not submit
  `/api/v1/relay/responses` before timeout.
- Users observed startup-time failures on first request instead of immediate bridge startup errors.

## Timeline
1. Browser sends POST `/api/v1/chat/completions`.
2. Relay accepts queue request via `/api/v1/relay/requests`.
3. Desktop compute-node bridge receives/decrypts API v1 E2EE work.
4. Relay repeatedly polls `/api/v1/relay/responses/retrieve` and receives `404`.
5. Relay returns `504` with `compute_node_timeout`.

## Root cause
`desktop-tauri/src-tauri/python/compute_node_bridge.py` previously gated startup on
`ComputeNodeRuntime.ensure_model_ready()`, which only ensures/downloads model files. Actual
runtime init happened later on first request via `ModelManager.get_llm_instance()` during
request processing, causing first-request cold initialization to consume timeout budget.

## Why existing tests missed it
Existing encrypted desktop bridge integration coverage uses fake/instant runtimes to verify
protocol and encryption behavior. That validated API v1 E2EE flow but did not validate startup
readiness semantics or cold runtime initialization ordering.

## Fix
- Added `ComputeNodeRuntime.ensure_api_v1_runtime_ready()` to:
  - ensure model assets are ready,
  - warm runtime via `get_llm_instance()`,
  - require callable non-streaming `create_chat_completion`,
  - annotate diagnostics state with `api_v1_runtime_ready`.
- Updated desktop bridge startup to require API v1 runtime warmup before emitting `started`
  or beginning relay register/poll loops.
- Warmup failure now fails closed with startup `type: "error"` and non-zero exit.

## Regression tests added
- Desktop bridge unit tests prove warmup occurs before first poll.
- Desktop bridge unit tests prove warmup failure prevents polling and returns startup error.
- Runtime unit tests cover warmup success and failure paths (`None`, missing
  `get_llm_instance`, missing/non-callable `create_chat_completion`).

## Manual verification steps (Windows/local relay)
1. Start relay with distributed provider configured.
2. Start desktop Tauri compute node bridge.
3. Confirm bridge logs indicate runtime warmup before first registration/poll.
4. Send UI chat request.
5. Confirm relay logs `/api/v1/relay/responses` `200` before `/api/v1/chat/completions` returns.
6. Confirm no `504` and no repeated retrieve-only `404` loop.
