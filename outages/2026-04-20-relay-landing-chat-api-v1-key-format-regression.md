# Outage: relay landing-page chat failed after API v1 key-format migration

- **Date:** 2026-04-20
- **Slug:** `relay-landing-chat-api-v1-key-format-regression`
- **Affected area:** relay landing page chat UI at `/` (`static/index.html` + `static/chat.js`)

## Summary
The relay landing page continued to render at `/`, but user chat messages could not complete end-to-end in local relay mode. A previous patch fixed server-key decoding but left a v2 streaming-first browser path and an incorrect API v1 client key encoding, so the page still produced inconsistent failures.

## Symptoms
- Console showed `Streaming chat completion failed: Error: Unknown streaming error` from `/api/v2/chat/completions` attempts.
- `/api/v1/chat/completions` returned `500` with `Failed to encrypt response`.
- The chat history showed: `Sorry, I encountered an issue generating a response. Please try again.`

## Impact
Local relay validation of the core browser chat journey was broken. Users and developers could not verify the `relay.py` + API v1 compute path through the landing-page UI, despite the page and endpoint docs appearing healthy.

## Root cause
1. `static/chat.js` still attempted API v2 streaming first, even though relay-path traffic for `v0.1.0` is API v1-only and non-streaming.
2. The browser sent `client_public_key` as the PEM body extracted between BEGIN/END markers instead of Base64-encoding the full PEM bytes expected by API v1 encryption.
3. API v1 response encryption then failed when trying to encrypt to that malformed key (`Failed to encrypt response`).

## Remediation
- Switched relay landing-page send flow to API v1 JSON chat completion only (no relay-path v2 streaming request).
- Updated browser `client_public_key` serialization to Base64-encode full PEM bytes so API v1 can encrypt responses correctly.
- Tightened e2e coverage to require API v1-only/non-streaming behavior and fail if `/api/v2/chat/completions` is called.

## Follow-up / prevention
- Keep relay landing-page chat tests asserting no `/api/v2/chat/completions` traffic.
- Keep architecture docs explicit that relay-path traffic in `v0.1.0` is API v1-only and non-streaming.
- Treat browser/client key-format mismatches as outage-level regressions whenever API encryption contracts change.
- See also: `outages/2026-04-20-relay-api-v1-chat-round-trip-regression.md` for the
  integration-coverage follow-up that added real relay+desktop bridge+browser CI coverage.
