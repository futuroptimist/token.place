# Outage: relay landing-page E2EE optional wording

## Summary
The relay landing page (`/`) described end-to-end encryption as an optional enhancement and showed an optional `"encrypted": true` pattern. That contradicted token.place policy: relay traffic must be relay-blind E2EE by design.

## Impact
- Risked implementers building against misleading guidance.
- Increased chance of future plaintext regressions by normalizing optional-E2EE language.

## Fix
- Rewrote the relay landing-page encryption section to state E2EE is always on.
- Replaced optional wording with ciphertext-only envelope guidance.
- Added regression test coverage asserting the mandatory-E2EE wording and guarding against optional-encryption text.

## Prevention
- Keep security-critical docs under test when they define protocol invariants.
- Treat plaintext examples on relay/API pages as release-blocking violations.
