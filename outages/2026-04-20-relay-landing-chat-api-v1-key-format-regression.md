# Outage: relay landing-page chat failed after API v1 key-format migration

- **Date:** 2026-04-20
- **Slug:** `relay-landing-chat-api-v1-key-format-regression`
- **Affected area:** relay landing page chat UI at `/` (`static/index.html` + `static/chat.js`)

## Summary
The initial fix only normalized server public-key format handling in `static/chat.js`, but left the relay landing page on a v2-streaming-first browser path. That meant relay-path traffic still attempted `/api/v2/chat/completions` before v1, and API v1 response encryption still failed for browser-formatted client keys.

## Symptoms
- Landing page loads normally at `http://127.0.0.1:5010/`.
- Sending a chat message from the landing-page composer does not produce a real assistant reply.
- The chat history shows: `Sorry, I encountered an issue generating a response. Please try again.`

## Impact
Local relay validation of the core browser chat journey was broken. Users and developers could not verify the `relay.py` + API v1 compute path through the landing-page UI, despite the page and endpoint docs appearing healthy.

## Root cause
1. API v1/v2 `GET /api/v1/public-key` and `GET /api/v2/public-key` return the server key as Base64-encoded PEM bytes.
2. The prior patch corrected server-key normalization but kept landing chat behavior as v2-streaming-first with v1 fallback.
3. The browser sends `client_public_key` as Base64 body content (without PEM headers), while `api/v1/encryption.py` decoded that field to DER bytes and passed those bytes directly to `encrypt.encrypt`, which requires PEM. This mismatch caused API v1 to return `500 Failed to encrypt response`.

## Remediation
- Added `normalizeServerPublicKey` in `static/chat.js` to accept both:
  - legacy/plain PEM key strings, and
  - API v1 Base64-encoded PEM key payloads (decoded before use).
- Updated relay landing-page send flow to use API v1 only and non-streaming behavior.
- Updated `api/v1/encryption.py` to normalize `client_public_key` into PEM before response encryption.
- Updated Playwright coverage to enforce relay landing chat API v1-only/non-streaming behavior and fail if `/api/v2/chat/completions` is called.
- Added API v1 encryption regression tests for Base64-body `client_public_key` formatting.

## Follow-up / prevention
- Preserve browser tests that explicitly validate key-format compatibility for landing-page crypto bootstrapping.
- Enforce API v1-only, non-streaming relay-path behavior in tests and contributor docs.
- Require outage notes for any future API key-format/transport changes that affect browser encryption initialization.
