# Outage: relay plaintext-field acceptance regression risk

- **Date:** 2026-05-27
- **Slug:** `relay-plaintext-acceptance-guardrail`
- **Affected area:** relay API v1 ciphertext envelope endpoints and relay landing-page guidance

## Summary
Relay API v1 envelope endpoints accepted payloads that could include plaintext-like fields in addition to ciphertext envelope fields. While relay routing primarily used ciphertext fields, this violated fail-closed expectations for relay-blind E2EE.

## Impact
- Client payloads could include plaintext-like keys without immediate relay rejection.
- This increased risk of accidental plaintext leakage in future integrations or regressions.

## Remediation
- Added fail-closed validation in `relay.py` for `/api/v1/relay/requests` and `/api/v1/relay/responses` to reject plaintext-like payload keys.
- Added regression tests to enforce ciphertext-only envelope contracts.
- Updated relay landing-page wording to remove optional/privacy framing and state E2EE is always on by design.

## Follow-up / prevention
- Keep relay envelope contract tests mandatory in CI.
- Treat any plaintext-bearing relay payload fields as protocol violations and reject with 400.
