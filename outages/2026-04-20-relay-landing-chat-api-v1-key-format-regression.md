# Outage: relay landing-page chat failed after API v1 key-format migration

- **Date:** 2026-04-20
- **Slug:** `relay-landing-chat-api-v1-key-format-regression`
- **Affected area:** relay landing page chat UI at `/` (`static/index.html` + `static/chat.js`)

## Summary
The relay landing page continued to render at `/`, but user chat messages could no longer complete end-to-end in local relay mode. The UI fell back to a generic assistant failure message instead of rendering a real model response.

## Symptoms
- Landing page loads normally at `http://127.0.0.1:5010/`.
- Sending a chat message from the landing-page composer does not produce a real assistant reply.
- The chat history shows: `Sorry, I encountered an issue generating a response. Please try again.`

## Impact
Local relay validation of the core browser chat journey was broken. Users and developers could not verify the `relay.py` + API v1 compute path through the landing-page UI, despite the page and endpoint docs appearing healthy.

## Root cause
1. API v1/v2 `GET /api/v1/public-key` and `GET /api/v2/public-key` return the server key as Base64-encoded PEM bytes.
2. Landing-page `static/chat.js` treated `public_key` as directly usable PEM and passed it unchanged to `JSEncrypt#setPublicKey`.
3. The prior fix restored key normalization but left a relay-path `/api/v2/chat/completions` streaming-first branch in place, which contradicts the v0.1.0 API v1-first rollout.
4. API v1 response encryption still assumed `client_public_key` strings were Base64 only. If a PEM-formatted key string reached the endpoint, response encryption failed with `500 Failed to encrypt response`.

## Remediation
- Added `normalizeServerPublicKey` in `static/chat.js` to accept both:
  - legacy/plain PEM key strings, and
  - API v1 Base64-encoded PEM key payloads (decoded before use).
- Updated `sendMessage` in `static/chat.js` to use API v1 non-streaming only for relay landing-page chat (no v2-first streaming attempt).
- Hardened API v1 response encryption key handling to accept both PEM strings and Base64 key strings for `client_public_key`.
- Updated Playwright coverage to fail if relay landing-page chat attempts `/api/v2/chat/completions`, and to assert `stream` is not set on `/api/v1/chat/completions`.
- Added unit coverage for API v1 encryption manager PEM-key compatibility.

## Follow-up / prevention
- Preserve browser tests that explicitly validate key-format compatibility for landing-page crypto bootstrapping.
- Keep relay landing-page chat tests enforcing API v1-only, non-streaming behavior for v0.1.0.
- Require outage notes for any future API key-format/transport changes that affect browser encryption initialization.
