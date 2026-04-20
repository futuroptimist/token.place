# Outage: relay landing-page chat failed after API v1 key-format migration

- **Date:** 2026-04-20
- **Slug:** `relay-landing-chat-api-v1-public-key-format-regression`
- **Affected area:** relay landing page chat UI (`/`) in local relay + compute-node flows

## Summary
The landing-page chat UI rendered correctly, but sending a message failed and fell back to the
assistant's generic failure copy instead of returning a real response.

## Symptoms
- Visiting `http://127.0.0.1:5010/` showed the chat widget normally.
- User messages were accepted in the UI, but no assistant response arrived.
- The UI displayed `Sorry, I encountered an issue generating a response. Please try again.`

## Impact
Local validation of relay landing-page chat was blocked. Operators could not verify end-to-end
chat behavior from the main relay page, despite the rest of the page loading successfully.

## Root cause
1. During API v1 migration, `/api/v1/public-key` standardized on base64-encoded DER output.
2. The landing-page browser client still treated `public_key` as PEM input for `JSEncrypt`.
3. The frontend's request encryption path failed because the server key format was incompatible,
   causing both streaming and non-streaming chat calls to fail.

## Remediation
- Updated landing-page chat key loading to normalize either PEM or base64 public keys to PEM
  before encryption.
- Added `public_key_pem` to API public-key responses (v1 and v2) while preserving existing
  `public_key` for compatibility.
- Added regression tests that assert the landing-page chat key-normalization path and public-key
  response shape.

## Follow-up / prevention
- Keep browser chat tests aligned with API key format contracts whenever key serialization changes.
- Preserve dual-format response compatibility (`public_key` + `public_key_pem`) for browser clients.
- Treat relay landing-page chat as a release gate for API surface migrations.
