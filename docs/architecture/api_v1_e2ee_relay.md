# API v1-only E2EE relay architecture (v0.1.0)

This note is the canonical architecture baseline for the API v1 E2EE migration roadmap.

## Encrypted inference progress sideband

Registration advertises `relay_capabilities.encrypted_progress_v1`. The compute node retains this
per relay and uses the compute-only `POST /api/v1/relay/progress` route only when that exact relay
advertises support. Either rollout order is safe: an older desktop sends no updates and a new
desktop treats a missing capability as disabled. There is no plaintext or legacy fallback.

The route requires registration authentication plus the exact registered server control
credential. Under the terminal-transition locks, the relay verifies ownership of the active
`(client_public_key, request_id)` pair. Its strict 16 KiB contract contains only those routing
identities, protocol/version, credential, ciphertext, encrypted content key, and IV. Phase,
counters, prompt, options, and output stay encrypted.

The freshly hybrid-encrypted client-bound plaintext is:

```json
{"protocol":"tokenplace_api_v1_relay_e2ee","version":1,"request_id":"opaque","client_public_key":"client key","api_v1_progress":{"schema_version":1,"sequence":1,"phase":"preparing","total_prompt_tokens":0,"cached_prompt_tokens":0,"processed_prompt_tokens":0,"generated_tokens":0,"elapsed_ms":0}}
```

Phases are `preparing`, `prefill`, or `generating`. Counters are non-negative safe integers and a
positive total requires `cached <= processed <= total`. A request-scoped publisher supplies a
monotonic sequence across worker recovery. Its single worker retains only the latest event,
coalesces same-phase bursts to roughly one update per second, sends phase changes promptly, and
uses bounded timeouts/backoff. Progress failure never fails inference.

The relay buffers at most one ciphertext envelope per active request. A pending response poll
atomically pops it as `encrypted_progress`. Cancellation, expiry, completion, unregister, eviction,
and all terminal transitions clear it; progress neither revives work nor renews deadlines or
accounting leases. The browser rechecks active request/client bindings around decryption, validates
the fixed schema and increasing sequence, and ignores malformed or stale events.

The landing chat renders a labelled native `<progress>` element. Waiting, Preparing, unknown
Prefill, and Generating are indeterminate; known Prefill uses token `value` and `max`. A polite,
atomic live region announces phase changes and coarse milestones, not each update. Terminal states
and teardown remove it. This sideband is not response-token streaming: API v1 still returns the
fully generated encrypted completion once and atomically. Telemetry failure cannot downgrade to
plaintext or affect completion.

## Release target and scope

- **API v1 is the active API for token.place v0.1.0.**
- **API v1 is non-streaming.** Responses are returned only after full model generation is
  complete.
- **Do not add streaming to API v1** for relay/client-server inference paths.
- **API v1 chat is text-only.** The v0.1.0 runtime target is a single Llama 3-family text
  model, not a multimodal model. Chat completion payloads must not accept, transform, summarize,
  placeholder, or otherwise pretend to support image content blocks such as `image_url`,
  `input_image`, or `image`; these requests must fail closed at validation/runtime boundaries.

## Runtime routing rules (must-follow)

All active production inference paths must use API v1 E2EE routes:

- `server.py` API/runtime inference paths
- `relay.py` relay paths
- `client.py` client paths
- `desktop-tauri` compute-node / bridge paths
- relay landing-page HTML chat UI served by `relay.py`

If a path cannot preserve API v1 E2EE invariants, it must **fail closed** instead of routing
plaintext or using deprecated fallbacks.

### Desktop runtime completion contract

API v1 desktop bridge generation must use the direct OpenAI-compatible runtime completion API:
`get_llm_instance().create_chat_completion(..., stream=False)`. This direct non-streaming
completion path is required even when the client sends `options: {}` or explicitly sends
`options: {"stream": false}`.

A desktop runtime that only exposes legacy chat-history helpers such as
`llama_cpp_get_response()` is **not** API v1-capable for relay inference. API v1 relay handling
must return an encrypted fail-closed error, such as `compute_node_model_unsupported`, rather
than silently falling back to legacy runtime behavior. Do not preserve, add, or suggest a legacy
runtime fallback for API v1 desktop relay requests.

## API v2 status

- API v2 exists in the repository, but it is currently incomplete.
- Do **not** route active runtime traffic through API v2 yet.
- Do **not** migrate server, relay, client, desktop, or relay HTML chat UI runtime paths to API v2
  until API v1 is launched and v0.1.0 is finalized.

## Deprecated legacy relay endpoints

The following endpoints are deprecated legacy relay routes:

- `/sink`
- `/faucet`
- `/source`
- `/retrieve`
- `/next_server`

Rules:

- Do not use them in active production inference paths.
- Do not extend them for new features.
- Do not reintroduce them as compatibility fallbacks in active runtime traffic.
- Use API v1 E2EE relay routes instead.

Legacy routes may remain temporarily for historical compatibility and migration staging, but they
must be clearly labeled deprecated legacy behavior in docs and code comments.

## E2EE invariant (relay-blind requirement)

Relay-visible surfaces must remain ciphertext-only plus safe routing metadata.

Relay-owned state, relay logs, relay diagnostics, and relay HTTP payloads must never include
plaintext model payload content, including:

- plaintext prompts
- OpenAI `messages`
- legacy `prompt` fields
- assistant response text
- tool arguments
- model output text or equivalent content payloads

Any path that would expose plaintext to relay-owned surfaces must fail closed.

## Migration context (why this exists)

There is a known alignment gap between `relay.py`, desktop-tauri flows, and the relay landing-page
HTML chat UI. Some end-to-end flow segments still hit deprecated legacy routes.

The migration roadmap follow-up phases own the implementation repair:

1. restore/audit API v1 relay/server route contract,
2. migrate desktop bridge paths,
3. migrate relay landing-page chat path and remove plaintext bypass behavior,
4. add final guardrails proving active production paths no longer use legacy routes.

This documentation baseline intentionally does **not** implement those code migrations.
