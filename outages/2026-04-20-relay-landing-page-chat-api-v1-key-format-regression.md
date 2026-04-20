# Outage: relay landing-page chat failed after API v1 migration key-format mismatch

- **Date:** 2026-04-20
- **Slug:** `relay-landing-page-chat-api-v1-key-format-regression`
- **Affected area:** relay landing page (`/`) chat UI and end-to-end local relay chat flow

## Summary
The relay landing page loaded successfully, but sending a message from the `/` chat UI failed and fell back to the generic assistant error response instead of returning a real completion.

## Symptoms
- Visiting `http://127.0.0.1:5010/` showed the chat UI as expected.
- Sending a chat message displayed the generic failure message (`Sorry, I encountered an issue generating a response. Please try again.`).
- Browser requests reached `/api/v1/public-key`, but follow-up encrypted chat requests failed before successful completion rendering.

## Impact
Local relay users lost the primary landing-page chat interaction and could not validate end-to-end chat from the relay homepage during development or smoke testing.

## Root cause
During the API v1 migration, the public key endpoint standardized on base64-encoded PEM (`public_key`) while the landing-page chat encryption path continued treating that field as a raw PEM string. This key-format mismatch caused client-side RSA setup to fail, which then prevented successful encrypted chat requests and produced the generic fallback error in the UI.

## Remediation
- Updated `static/chat.js` to normalize server public keys by accepting either raw PEM or base64-encoded PEM from `/api/v1/public-key`.
- Kept the landing-page chat on the API v1/v2 architecture (no rollback to legacy relay transport):
  - key fetch remains `/api/v1/public-key`
  - streaming/non-stream chat routes remain `/api/v2/chat/completions` and `/api/v1/chat/completions`
- Added a focused UI regression test that mocks a base64-encoded API v1 public key and verifies the landing-page chat sends a real request and renders a real assistant response.

## Follow-up / prevention
- Keep regression coverage for base64-formatted API v1 public keys in the landing-page chat path.
- Preserve compatibility in the chat client for both PEM and base64 PEM input formats when API key serialization changes.
- Include relay landing-page chat send/receive checks in local release smoke tests.
