# API v1-only E2EE relay architecture (v0.1.0 baseline)

## Status baseline for Prompt 0

This document is the evergreen architecture baseline for the API v1-only relay repair
sequence (Prompts 1-4).

- **API v1 is the active API for v0.1.0.**
- **API v1 is non-streaming.** Responses must be returned only after the full model response is
  generated.
- **Do not add streaming to API v1.**
- **API v2 exists but is incomplete.** Do not route active runtime traffic through API v2 yet.
- Do not migrate UI, desktop, relay, or server runtime paths to API v2 until API v1 is fully
  launched and `v0.1.0` is finalized.

## Required runtime alignment

Active inference paths for `v0.1.0` must align on API v1 E2EE routes:

- `server.py`
- `relay.py`
- `client.py`
- desktop Tauri app
- relay.py landing-page HTML chat UI

Legacy relay routes (`/sink`, `/faucet`, `/source`, `/retrieve`, `/next_server`) are
**deprecated legacy endpoints**. They may remain temporarily for migration compatibility, but:

- do not use them for active production inference paths,
- do not extend them for new features,
- do not reintroduce them as fallback behavior.

## E2EE invariant (fail-closed)

Relay-owned systems are relay-blind by design. Relay-visible data may include only ciphertext and
safe routing metadata.

The relay must never store, log, inspect, diagnose, or forward plaintext model payload content,
including prompts, OpenAI `messages`, legacy `prompt`, assistant responses, tool arguments, or
model output text.

If any path cannot preserve this E2EE invariant, that path must fail closed.

## Migration context

There is a known gap between `relay.py`, the desktop Tauri app, and the relay landing-page chat UI:
some E2E flow segments still use deprecated legacy routes.

Prompts 1-4 repair this by restoring and aligning active runtime paths on API v1 E2EE routes
without prematurely migrating to API v2.
