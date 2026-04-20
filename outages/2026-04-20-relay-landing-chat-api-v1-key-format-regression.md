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
3. The resulting client-side encryption path failed, so both streaming and non-stream chat attempts failed before a valid completion payload could be processed.

## Remediation
- Added `normalizeServerPublicKey` in `static/chat.js` to accept both:
  - legacy/plain PEM key strings, and
  - API v1 Base64-encoded PEM key payloads (decoded before use).
- Updated `getServerPublicKey` to normalize the API response before storing `serverPublicKey`.
- Added Playwright regression coverage to assert the landing page can:
  - fetch a Base64 public key,
  - fail over from `/api/v2/chat/completions` streaming failure,
  - and still receive/render a real assistant response from `/api/v1/chat/completions`.

## Follow-up / prevention
- Preserve browser tests that explicitly validate key-format compatibility for landing-page crypto bootstrapping.
- Keep relay landing-page chat tests exercising both `/api/v2/chat/completions` and `/api/v1/chat/completions` fallback behavior.
- Require outage notes for any future API key-format/transport changes that affect browser encryption initialization.
