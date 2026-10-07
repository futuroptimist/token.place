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

## Current trust boundary

The ciphertext-only invariant assumes an honest relay distributing the intended compute key.
Current discovery does not independently authenticate compute identity, and encryption to the
client public key does not authenticate the response sender. The
[K061 design](../design/verified-compute-trust.md) proposes independently provisioned operator
roots and authenticated transcripts coordinated with K069; these are not implemented guarantees.
A relay serving mutable client JavaScript can also replace verification code.

## Landing-page system message

The landing-page chat gives the model a small, factual description of token.place so it can answer
product questions without inventing capabilities. The exact message is this single-line value:

```text
You are the assistant in the token.place landing-page chat. Answer product questions using these facts, and distinguish implemented behavior from proposed designs and unverified deployment details. token.place connects people requesting generative-AI inference with people contributing compute. A selected compute node runs the model. The client encrypts request context for that node, which decrypts it and encrypts its response for the client. Retained conversation context may accompany later turns and failover to another node. Compatible self-hosted HTTPS relays are supported; token.place is not the only permitted relay hostname. Configurable compute nodes can target a chosen relay, which may require admission credentials. This landing page uses its serving origin; do not invent configuration controls. Active API v1 chat is text-only and non-streaming. Progress indicators describe processing status, not partial answer text. API v2 is incomplete. Model availability, context and output limits, speed, and capacity depend on configuration and eligible compute nodes. Do not promise a particular live model, node count, response time, or successful request. The relay-blind goal keeps conversation plaintext out of relay-owned payloads, state, logs, and diagnostics. Current protection assumes an honest relay distributes the intended compute key. Clients currently trust relay-selected keys; independent compute identity and response-sender authentication are not implemented guarantees. Compute operators receive plaintext and may retain it. Routing metadata remains visible. A relay serving mutable client JavaScript can replace that code. Proposed independent operator trust and authenticated transcripts are future work, not current protection. Requests can fail because suitable capacity is unavailable, limits are exceeded, nodes disconnect, or deadlines expire. Suggest actions consistent with the displayed error, such as shortening context, choosing an available tier, or retrying later. Do not promise immediate cancellation or successful failover. If you are uncertain, say so. Do not invent pricing, retention policies, supported features, deployment status, or current availability. Do not claim or imply that you searched or browsed the web.
```

This wording deliberately limits the privacy claim to the relay boundary: the relay sees ciphertext
and safe routing metadata, but the selected compute node necessarily receives plaintext request
context for inference. It does not claim that the model has current information or that the compute
node cannot access or retain plaintext.

### Grounded FAQ scope and budget (K232)

The instruction is at most **3,072 UTF-8 bytes**, enforced by
`test_landing_faq_prompt_has_bounded_utf8_budget` in `tests/unit/test_touch_ui.py`.
This is a maintenance budget for the fixed instruction, not a tokenizer count or an increase
to any context/output limit. The entire instruction still participates in context estimation
and compute-side admission. Keep volatile model names, version numbers, capacity counts, timing
promises, and deployment observations out of this static prompt. Repository support is not proof
that a particular relay has deployed that behavior.

The factual boundaries come from these implementation and architecture sources:

| FAQ area | Source and boundary |
| --- | --- |
| Request flow and retained history | `static/chat.js`: `createApiV1Messages`, `sendMessageApiOnce`, and bounded failover reuse the conversation context. The selected compute node receives plaintext. |
| Self-hosted relays and compute admission | [README admission credentials](../../README.md#relay-compute-admission-credentials), `server.py`, and `utils/networking/relay_credentials.py`: configurable relays, destination-bound admission credentials, no token.place-only allowlist. This does not create a packaged desktop credential UI or a relay selector in the landing page. |
| API and model limitations | This document's API v1 baseline and `static/chat.js`: text-only, non-streaming, capability/tier-dependent requests. A model catalogue entry is not a guarantee of eligible live capacity. |
| Progress and failures | This document's encrypted-progress contract and `static/chat.js`: telemetry is not answer streaming; cancellation can remain unconfirmed, and retries/failover do not guarantee success. |
| Privacy and future designs | [K061/K069 design](../design/verified-compute-trust.md): the honest-relay/key-selection assumption remains current. Independent operator trust, authenticated transcripts, and trusted-client distribution are proposed protections, not shipped guarantees. |

Offline regression tests verify documentation/constant equality, the byte budget, required factual
qualifications, a single leading request-only instruction, context estimation, retained history,
retry/failover preservation, and encrypted relay boundaries. Browser scenarios use controlled
responses; these checks do **not** measure generated-answer accuracy or prove a deployed version.
No model inference is needed for these focused regressions.

Use the following answer rubric for a separately authorized future model evaluation. Accept
accurate paraphrases; fail unsupported guarantees even if the answer otherwise sounds helpful.
Record the exact model/build, prompt revision, settings, and outputs before claiming answer quality.

| Example question | Required answer boundary |
| --- | --- |
| What is token.place, and who runs my request? | Explain clients, relays, and selected compute nodes; do not claim the relay runs the model. |
| Can I use my own relay or contribute compute? | Support compatible self-hosted relays and configurable compute nodes; distinguish relay admission from client verification of compute identity. |
| Can this page switch to my relay? | Do not invent a control: the landing page uses its serving origin. |
| Can I send images or stream the answer? | API v1 chat is text-only and non-streaming; progress is status telemetry. |
| Which model is online, and how fast will it answer? | Do not infer current model availability, node counts, latency, or capacity from this static instruction. |
| Can a malicious relay or the compute operator read my prompt? | State the honest-relay/key-selection assumption, compute plaintext access, and mutable-client-code risk; do not promise malicious-relay resistance or non-retention. |
| Are independently verified compute and signed responses implemented? | Distinguish the K061/K069 proposal from current behavior. |
| Why did my request fail, and does cancel stop it immediately? | Follow the displayed error; explain limits, capacity, disconnects, deadlines, and potentially unconfirmed cancellation without promising recovery. |
| Will another node see earlier turns after failover? | Retained context can accompany failover to another selected compute node. |
| Is this version deployed, always available, free, or based on a web search? | Admit missing evidence; do not invent deployment, availability, pricing, retention policy, or browsing claims. |

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
