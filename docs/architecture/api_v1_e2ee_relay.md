# API v1-only E2EE relay architecture (v0.1.0)

This note is the canonical architecture baseline for the API v1 E2EE migration roadmap.

## Release target and scope

- **API v1 is the active API for token.place v0.1.0.**
- **API v1 is non-streaming.** Responses are returned only after full model generation is
  complete.
- **Do not add streaming to API v1** for relay/client-server inference paths.
- **API v1 chat is text-only.** The canonical runtime target is Qwen3 8B Q4_K_M, exposed as
  `qwen3-8b-instruct`, not a multimodal model. Chat completion payloads must not accept, transform,
  summarize, provide placeholders for, or otherwise pretend to support image content blocks such
  as `image_url`, `input_image`, or `image`; these requests must fail closed at validation/runtime
  boundaries.

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

## Landing-page system message

The landing-page chat gives the model a small, factual description of token.place so it can answer
product questions without inventing capabilities. The message is:

> You are the assistant in the token.place landing-page chat. token.place connects people who need
> generative-AI inference with people who contribute compute; a selected compute node serves each
> request. The relay routes end-to-end encrypted request and response envelopes and cannot read the
> conversation, while the selected compute node decrypts the request to run inference and encrypts
> its response for the requesting browser. If you are uncertain, say so. Do not claim or imply that
> you searched or browsed the web.

This wording deliberately limits the privacy claim to the relay boundary: the relay sees ciphertext
and safe routing metadata, but the selected compute node necessarily receives plaintext request
context for inference. It does not claim that the model has current information or that the compute
node cannot access or retain plaintext.

### Request construction

The relay-served landing-page client (`static/chat.js`) constructs, for every outgoing API v1
conversation, a fresh request-message array by prepending one
copy of the system message to the user/assistant conversation returned by the current request
builder. That complete array, including the system message, must be used both for automatic
context-tier estimation and for `api_v1_request.messages` before the whole request envelope is
encrypted to the selected compute node. The same complete array must be reused by context-tier
retries and compute-node failover retries.

The system message must not be stored in or reconstructed from display `chatHistory`. That state is
currently filtered into only `user` and `assistant` messages when API v1 request context is built.
Keeping the request-only instruction separate prevents rendering it as conversation history while
ensuring that each later turn reconstructs it ahead of the retained user/assistant conversation.

This is per-request context delivered only to the compute node selected for that encrypted request;
it is not persistent model configuration and is not propagated to every registered node. Therefore,
the behavior belongs to the relay's landing-page static asset, not relay routing, compute node
registration, model packaging, or node configuration. The compute-node API v1 validator already
accepts `system`, `user`, and `assistant` roles, and the inference path passes the validated message
list through runtime preparation, context admission, and non-streaming chat completion.

### Implemented invariants

- Every landing-page API v1 request contains exactly one copy of the system message, first
  in the encrypted conversation message list.
- The message remains present exactly once on subsequent turns, automatic context-tier retries, and
  compute-node failover retries, without entering the visible chat history.
- Context-tier estimation includes the message, and the message is inside the E2EE request envelope;
  relay-owned payloads, state, logs, and diagnostics remain ciphertext-only plus safe routing
  metadata.
- A focused end-to-end landing-page check sends an initial prompt, a subsequent turn, and a retry,
  verifies the selected compute node receives exactly one system message each time, and confirms the
  relay never receives plaintext conversation content.

## Migration context (why this exists)

There is a known alignment gap between `relay.py`, desktop-tauri flows, and the relay landing-page
HTML chat UI. Some end-to-end flow segments still hit deprecated legacy routes.

The migration roadmap follow-up phases own the implementation repair:

1. restore/audit API v1 relay/server route contract,
2. migrate desktop bridge paths,
3. migrate relay landing-page chat path and remove plaintext bypass behavior,
4. add final guardrails proving active production paths no longer use legacy routes.

This documentation baseline intentionally does **not** implement those code migrations.

## Encrypted inference progress sideband

Registration responses advertise `relay_capabilities.encrypted_progress_v1`. A compute node retains
that capability for the exact relay registration and otherwise does not publish progress. This makes
new-relay/old-desktop and old-relay/new-desktop rollout orders safe, with ordinary inference retained.
There is no plaintext or legacy-route fallback.

A capable compute node sends `POST /api/v1/relay/progress` using registration authentication and the
exact server control credential. The relay verifies that server owns the active client/request pair
under the terminal-transition locks. The strictly allowlisted outer envelope contains only routing
identities, protocol/version, and `ciphertext`, `cipherkey`, and `iv`. Phase and counters occur only in
the hybrid-encrypted inner envelope:

```json
{"protocol":"tokenplace_api_v1_relay_e2ee","version":1,"request_id":"opaque","client_public_key":"recipient","api_v1_progress":{"schema_version":1,"sequence":1,"phase":"preparing","total_prompt_tokens":0,"cached_prompt_tokens":0,"processed_prompt_tokens":0,"generated_tokens":0,"elapsed_ms":0}}
```

Phases are `preparing`, `prefill`, or `generating`; counters are non-negative safe integers and known
prompt totals bound cached and processed counts. A request-owned publisher serializes only these
fields, assigns an external monotonic sequence independent of worker generations, encrypts each
update with fresh AES key/IV material to the requesting browser key, and performs bounded network
work off the inference callback. It keeps only the latest value, coalesces bursts at about one update
per second, and stops on terminal lifecycle results. Failures are best-effort and never fail inference.

The relay stores at most one ciphertext update per active client/request pair. A pending response poll
atomically pops it as `encrypted_progress`; terminal completion, cancellation, expiration,
unregistration, eviction, and retrieval cleanup discard it. Progress neither renews the authoritative
deadline/accounting lease nor completes a request. Terminal locking prevents late progress from
reviving work.

The landing client verifies both outer and decrypted inner identities and protocol versions, validates
the fixed schema, and accepts only increasing sequences for its current request. Invalid progress is
ignored. A native `<progress>` is indeterminate while waiting, preparing, or generating, determinate
only for prefill with a positive total, and is removed for every terminal transition. Visible labeling,
descriptive text, and a polite atomic live region announce phase changes and coarse milestones rather
than every counter update.

This is encrypted telemetry, not token streaming: API v1 still publishes the assistant response once,
after full generation and encryption. Progress failure never exposes plaintext and never delays,
changes, or fragments that atomic completion.
