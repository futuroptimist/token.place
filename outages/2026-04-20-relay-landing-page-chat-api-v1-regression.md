# Outage: relay landing-page chat failed after API v1 migration

- **Date:** 2026-04-20
- **Slug:** `relay-landing-page-chat-api-v1-regression`
- **Affected area:** relay landing page chat UI at `/` (`static/index.html` + `static/chat.js`) when connected to local v0.1.0 API paths

## Summary
The relay landing page rendered, but user chat messages failed end-to-end and the UI fell back to the generic assistant failure text instead of returning a real model response.

## Symptoms
- Opening `http://127.0.0.1:5010/` showed the chat widget normally.
- Sending a message produced `Sorry, I encountered an issue generating a response. Please try again.`
- Network requests reached `/api/v1/chat/completions` and `/api/v2/chat/completions`, but response decryption in the browser failed.

## Impact
Local relay demos and operator verification flows could no longer validate the core landing-page chat journey, even though the rest of the v0.1.0 API surface was reachable.

## Root cause
- During the v0.1.0 migration, API responses for encrypted chat payloads began RSA-wrapping AES session keys using OAEP defaults in backend encryption helpers.
- The landing-page browser client still relies on JSEncrypt for private-key operations, which expects PKCS#1 v1.5 wrapped session keys.
- This padding mismatch caused client-side decrypt failures, which then surfaced as the generic assistant error in the UI.

## Remediation
- Updated API encrypted response key-wrapping to use PKCS#1 v1.5 compatibility mode for browser chat clients.
- Updated v2 encrypted streaming chunk generation to use the same key-wrapping mode for first-chunk session keys.
- Added regression tests that assert encrypted response/session keys are decryptable via PKCS#1 v1.5 client behavior.

## Follow-up / prevention
- Keep landing-page chat compatibility checks in API encryption regression tests (non-stream + streaming).
- Add a browser smoke test in CI that validates a real message/response cycle from `/` through API v1/v2 endpoints.
- Treat RSA padding changes as compatibility-sensitive API changes requiring explicit migration notes.
