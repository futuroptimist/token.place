# API v1 desktop bridge cold runtime timeout (May 21, 2026)

## Summary
The API v1 encrypted desktop relay path could time out on the first browser request because the desktop bridge reported readiness before the llama.cpp runtime was actually initialized.

## Impact
- Browser requests to `/api/v1/chat/completions` could return `504 compute_node_timeout` after ~30 seconds.
- Relay accepted and queued API v1 E2EE requests but did not receive a response envelope in time.
- Desktop bridge had already registered and was polling, but first request still paid cold runtime initialization.

## Timeline
1. Browser `POST /api/v1/chat/completions` started.
2. Relay accepted queue write via `POST /api/v1/relay/requests`.
3. Desktop bridge polled and received API v1 E2EE work; request was decrypted in desktop runtime.
4. Relay repeatedly polled `POST /api/v1/relay/responses/retrieve` and saw `404` (not ready yet).
5. Relay timed out and returned `504 compute_node_timeout` to browser.

## Root cause
`desktop-tauri/src-tauri/python/compute_node_bridge.py` used `runtime.ensure_model_ready()` and then emitted startup readiness, but that check only verified model availability/download. Actual llama.cpp instance initialization happened later through `ModelManager.get_llm_instance()` during request processing, inside the distributed timeout window.

## Why tests missed it
Existing encrypted desktop bridge E2E coverage used fake/instant desktop runtimes. That validated API v1 protocol/encryption invariants, but not readiness semantics against cold runtime initialization latency.

## Fix
- Added `ComputeNodeRuntime.ensure_api_v1_runtime_ready()` that:
  - runs `ensure_model_ready()`,
  - forces runtime init via `model_manager.get_llm_instance()`,
  - validates callable `create_chat_completion`,
  - fails closed on any warmup/validation failure.
- Updated desktop bridge startup to require API v1 runtime warmup before `started` emission and before relay register/poll loop.

## Regression tests added
- Unit coverage for `ensure_api_v1_runtime_ready()` success/failure paths.
- Desktop bridge startup tests verifying warmup failure is surfaced as immediate startup error.
- Desktop bridge startup ordering regression that asserts warmup occurs before polling.

## Manual verification (Windows/local relay)
1. Start relay with distributed provider configured.
2. Start desktop Tauri compute node.
3. Confirm startup logs show model init and runtime warmup before first registration/poll.
4. Send a chat message from UI.
5. Confirm relay logs `POST /api/v1/relay/responses` 200 before `/api/v1/chat/completions` returns.
6. Confirm no browser 504 and no repeated retrieve-only 404 loop.
