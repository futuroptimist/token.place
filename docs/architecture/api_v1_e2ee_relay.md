# API v1-only E2EE relay architecture (v0.1.0)

This note is the canonical architecture baseline for the API v1-only relay repair sequence
(Prompts 0-4).

## Launch target and active runtime contract

- **API v1 is the active API for `v0.1.0`.**
- **API v1 is non-streaming.** Responses are returned only after the full model response is
  generated.
- **Do not add streaming to API v1.**
- All active runtime inference paths must target API v1 routes:
  - `server.py`
  - `relay.py`
  - `client.py`
  - desktop Tauri app (`desktop-tauri/`)
  - relay landing-page HTML chat UI served by `relay.py`

## API v2 status (do not use yet)

- API v2 exists in-repo but is currently incomplete for launch.
- Do **not** route active runtime traffic through API v2 yet.
- Do **not** migrate UI, desktop, relay, or server active inference flows to API v2 until API v1
  is launched and the `v0.1.0` tag is finalized.

## Legacy relay route deprecation (must not be active)

The following endpoints are deprecated legacy relay routes:

- `/sink`
- `/faucet`
- `/source`
- `/retrieve`
- `/next_server`

Rules:

- Do not use them in active production paths.
- Do not extend them for new features.
- Do not reintroduce them as compatibility fallbacks.
- Use API v1 E2EE relay routes instead.

Legacy endpoints may remain temporarily for migration compatibility and historical context only,
with explicit deprecated/legacy labeling.

## E2EE relay invariant (fail closed)

All client/server communication through relay must remain relay-blind E2EE:

- Relay sees ciphertext plus safe routing metadata only.
- Plaintext prompts/messages/responses/tool arguments/model output must never appear in
  relay-owned state, relay logs, relay diagnostics, or relay-visible HTTP payloads.
- If a path cannot preserve E2EE, it must fail closed.

## Migration context for Prompts 1-4

Known current gap: `relay.py`, desktop Tauri, and the relay HTML chat UI are not fully aligned on
API v1 E2EE. Some end-to-end path segments still mistakenly use legacy routes.

Prompt sequence intent:

1. Prompt 0 (this docs baseline): lock architecture direction.
2. Prompt 1: restore API v1 relay/server route contract.
3. Prompt 2: migrate desktop/Tauri off legacy polling to API v1.
4. Prompt 3: migrate relay landing-page UI path to API v1 E2EE and remove plaintext/bypass
   behavior.
5. Prompt 4: add final guardrails/tests/docs proving active production paths no longer touch
   deprecated legacy routes.
