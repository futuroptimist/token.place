# Outage: relay landing-page chat failed after API v1 key-format migration

- **Date:** 2026-04-20
- **Slug:** `relay-landing-chat-api-v1-key-format-regression`
- **Affected area:** relay landing page chat UI at `/` (`static/index.html` + `static/chat.js`)

## Summary
The relay landing page continued to render at `/`, but user chat messages could no longer complete end-to-end in local relay mode. The UI fell back to a generic assistant failure message instead of rendering a real model response. A first remediation fixed server public-key normalization but left an incorrect relay-path transport assumption (`/api/v2` streaming-first with `/api/v1` fallback).

## Symptoms
- Landing page loads normally at `http://127.0.0.1:5010/`.
- Sending a chat message from the landing-page composer does not produce a real assistant reply.
- The chat history shows: `Sorry, I encountered an issue generating a response. Please try again.`

## Impact
Local relay validation of the core browser chat journey was broken. Users and developers could not verify the `relay.py` + API v1 compute path through the landing-page UI, despite the page and endpoint docs appearing healthy.

## Root cause
1. API v1/v2 `GET /api/v1/public-key` and `GET /api/v2/public-key` return the server key as Base64-encoded PEM bytes.
2. Landing-page `static/chat.js` treated `public_key` as directly usable PEM and passed it unchanged to `JSEncrypt#setPublicKey`.
3. The resulting client-side encryption path failed, producing API v1 `Failed to encrypt response` failures because `client_public_key` and encrypted payload handling no longer matched the server-side response-encryption contract.
4. The prior patch still attempted `/api/v2/chat/completions` streaming for relay-path chat before calling API v1. That contradicted `v0.1.0` architecture (relay-path must be API v1-only, non-streaming), and made relay failures noisy and confusing.

## Remediation
- Added `normalizeServerPublicKey` in `static/chat.js` to accept both:
  - legacy/plain PEM key strings, and
  - API v1 Base64-encoded PEM key payloads (decoded before use).
- Updated `getServerPublicKey` to normalize the API response before storing `serverPublicKey`.
- Removed relay landing-page `/api/v2/chat/completions` streaming attempts from the send-message flow.
- Kept relay landing-page chat on API v1 encrypted request/response only (non-streaming) so browser payloads stay aligned with the API v1 response-encryption contract.
- Updated Playwright regression coverage to assert:
  - landing chat sends encrypted API v1 requests,
  - no `stream` flag is sent,
  - no `/api/v2/chat/completions` request is attempted.

## Follow-up / prevention
- Preserve browser tests that explicitly validate key-format compatibility for landing-page crypto bootstrapping.
- Keep relay landing-page chat tests enforcing API v1-only, non-streaming behavior for relay-path traffic.
- Require outage notes for any future API key-format/transport changes that affect browser encryption initialization.
