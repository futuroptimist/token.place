# Security review checklist

Use this checklist before major releases, infrastructure changes, or when onboarding new relay
operators. Each section targets safeguards explicitly called out in the token.place polish plan.

## Relay failovers
- verify failover pairs in `config.json` and `.env` stay current with the deployed relay hosts.
- Confirm health probes and circuit breakers surface relay outages within 1 minute.
- Ensure incident docs describe how to promote a standby relay without exposing plaintext traffic.

## Cloudflare fallback
- document fallback owner responsible for maintaining tunnel credentials and DNS records.
- Validate Zero Trust policies restrict tunnel access to the relay IP allowlist.
- Confirm fail-open modes keep encryption enforced when Cloudflare is bypassed.

## Key management
- rotate operator keys on the cadence recorded in the latest security audit entry and document the
  change in `docs/SECURITY_PRIVACY_AUDIT.md`.
- Verify revoked keys are removed from cache stores (`utils/crypto/key_cache.py`).
- Check deployment automation wipes prior private keys from filesystem snapshots.

## Secrets boundaries
- Ensure `.env` samples and deployment manifests exclude production secrets.
- Confirm the relay never logs request ciphertext or decrypted content even during debug runs.
- Re-run the repository's secret-scanning workflow (for example `detect-secrets` on staged
  files) against configuration overrides introduced in the release.

## Logging redaction
- Confirm structured logs omit prompt/response payloads while preserving timestamps and request IDs.
- Ensure redaction filters handle both plaintext and base64 payload formats.
- Verify crash dumps scrub tokens, API keys, and relay invitation secrets.

## Audit steps
- Export the latest `run_all_tests.sh` report and attach it to the audit notes.
- Capture hashes for Docker images and Python wheels promoted to production.
- File a follow-up issue for any gaps discovered while running this checklist.
