# Security and Privacy Audit Log

This log records security and privacy audits performed on `token.place`.
Each entry includes the date of the audit, the Git commit hash that was
reviewed, and a summary of improvements or outstanding issues. Future audits
should append a new entry following the template below.

## How to Add a New Entry

```
### [YYYY-MM-DD] - commit <hash>

**Summary**
A short overview of the audit scope and results.

**Completed Improvements**
- Item 1
- Item 2

**Recommendations**
- Item 1
- Item 2
```

## Audit History

### [2025-03-28] - commit 62eb77c2f0b0daf73767e2e66d901a8358024ebb

**Summary**
Initial audit establishing baseline security and privacy posture.

**Completed Improvements**
- Implemented environment-aware logging that only logs during development/testing.
- Added `ENVIRONMENT` checks throughout the codebase to prevent logging in production.
- Created helper functions (`log_info`, `log_warning`, `log_error`) that automatically respect the environment setting.
- Replaced all direct print/logger calls with environment-aware versions.
- Added a null handler for production environments to suppress all logs.
- Fixed port configuration to use consistent testing ports (5010 for relay, 5020 for server).
- Verified correct implementation of the RSA–AES hybrid encryption scheme.
- Ensured proper handling of encryption keys and secure transmission.
- Improved test reliability by explicitly passing mock LLM flags and fixing encoding issues.
- Hardened chat completion validation to reject empty message arrays, closing the open
  input-validation recommendation.
- Redacted relay registration tokens from saved configs and documented the
  `TOKEN_PLACE_RELAY_SERVER_TOKEN` environment variable workflow.

**Recommendations**
- ✅ (2025-10-23) Added OpenAI-style JSON rate limit responses with `Retry-After` headers,
  verified by `tests/unit/test_rate_limit.py::test_rate_limit_uses_openai_style_error_payload`.
- Streaming implementation delivered (2025-09-30); continue planning for key rotation and a dedicated cryptographic audit.
- Consider zero-knowledge architecture, formal verification, and an external security review.

**Privacy Enhancements**
- No production logging.
- Minimized data storage; conversation data is not persisted.
- Encryption and decryption occur client-side.
- No analytics or usage tracking.

**Security Testing**
- Unit tests cover core cryptographic functions.
- Integration tests verify that components work together securely.
- End-to-end tests confirm encryption throughout the workflow.

**Compliance Considerations**
- GDPR readiness through minimal data collection.
- CCPA compatibility for the same reason.
- Foundation for HIPAA compliance via end-to-end encryption.

### [2025-06-17] - commit 33bd9ef8546b6e9331c4d72ff056a2c528f8de7f

**Summary**
Project structure reorganization and documentation updates.

**Completed Improvements**
- Moved configuration files into `config/` for clarity.
- Updated test references to reflect new paths.

**Recommendations**
- Continue to monitor for broken links or outdated paths after large refactors.

### [2025-08-09] - commit TBD

**Summary**
Refined logging helpers to avoid swallowing system interrupt exceptions.

**Completed Improvements**
- Updated `log_info` and `log_error` to catch only standard exceptions, allowing
  `KeyboardInterrupt` and `SystemExit` to propagate.
- Ensured `log_error` always logs messages even in production.
- Enforced a minimum 2048-bit RSA key size guard to block insecure configurations.
- Added configurable content moderation hooks that reject disallowed prompts before inference.

**Recommendations**
- Continue monitoring logging utilities for unintended side effects.

### [2025-10-10] - commit TBD

**Summary**
Integrated automated static analysis to provide a repeatable external security
review signal during CI runs.

**Completed Improvements**
- Added a pytest-backed Bandit scan (`tests/test_security_bandit.py`) that enforces zero
  medium/high severity findings.
- Configured the relay client's networking helpers to use explicit request timeouts and
  stricter unspecified-host detection.
- Registered a `security` pytest marker so the scan can be targeted independently when
  triaging failures.

**Recommendations**
- Periodically supplement automated scans with human-led reviews to capture logic flaws
  beyond static analysis.

### [2025-10-30] - commit TBD

**Summary**
Closed the outstanding dependency-audit action item by introducing a lightweight
requirements baseline check.

**Completed Improvements**
- Added `utils/security/dependency_audit.py` plus regression tests to enforce minimum
  secure versions for high-risk dependencies such as `requests`, `urllib3`, `Flask`,
  `httpx`, and `cryptography`.
- Wired the helper into the pytest suite via `tests/unit/test_dependency_audit.py`,
  ensuring regressions are caught in CI.
- Expanded the dependency baseline to include `tqdm` (GHSA-g7vv-2v7x-gj9p) and
  `idna` (PYSEC-2024-60) so archive handling and punycode validation advisories stay
  patched.

**Recommendations**
- Continue monitoring automated feeds (e.g., GitHub Security Advisories) for
  ecosystem-wide alerts beyond the covered packages.
