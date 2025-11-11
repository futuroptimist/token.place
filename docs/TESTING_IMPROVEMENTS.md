# Testing Improvement Ideas

This document serves as a scratch pad for potential testing improvements to implement in the token.place project.

## ✅ 1. End-to-End Tests (IMPLEMENTED)

**IMPLEMENTED in `tests/test_e2e_conversation_flow.py::test_manual_encrypted_conversation_flow_matches_docs_example`, which:**

- Exercises the documented manual workflow of fetching the server key, encrypting a user prompt,
  dispatching it to `/api/v1/chat/completions`, and decrypting the encrypted assistant reply.
- Verifies that the decrypted response originates from the mock LLM, proving the round-trip
  succeeded end-to-end.
- Ensures `ClientSimulator.encrypt_message` normalises string prompts into chat message payloads
  so developers can copy the documented snippet directly into their own tests or tooling.

## ✅ 2. Performance Benchmarks (IMPLEMENTED)

**IMPLEMENTED in tests/test_performance_benchmarks.py, which includes:**
- Benchmarking encryption with different payload sizes (1KB, 10KB, 100KB)
- Benchmarking decryption performance
- Testing key generation performance
- Measuring performance with realistic JSON payloads
- Handling large payloads and identifying performance bottlenecks

## ✅ 3. Failure and Recovery Testing (IMPLEMENTED)

**IMPLEMENTED in tests/test_failure_recovery.py and tests/test_crypto_failures.py, which include:**
- Testing decryption with invalid keys
- Testing server recovery after encryption errors
- Testing handling of missing required fields
- Testing resilience against corrupted ciphertext
- Testing API handling of malformed JSON
- Testing with missing data fields
- Testing with corrupted encryption/decryption inputs

## ✅ 4. Security Tests (IMPLEMENTED)

**IMPLEMENTED in tests/test_security.py, which includes:**
- Testing key uniqueness and randomness
- Testing encryption non-determinism (different ciphertexts for same plaintext)
- Testing IV uniqueness across encryptions
- Testing forward secrecy (one compromised message doesn't affect others)
- Testing padding oracle attack resistance
- Testing ciphertext integrity (modifications cause decryption failure)

## ✅ 5. Parameterized Tests (IMPLEMENTED)

**IMPLEMENTED in tests/test_parameterized.py, which includes:**
- Testing with different RSA key sizes (1024, 2048, 4096)
- Testing with different Unicode strings (multiple languages, emojis, special chars)
- Testing with various JSON object structures
- Testing with different character encodings (UTF-8, ASCII, Latin-1, UTF-16)
- Testing with different payload sizes
- Testing with binary data
- Testing encryption compatibility between different key sizes

## ✅ 6. Cross-Platform Browser Tests (IMPLEMENTED)

**IMPLEMENTED in `tests/test_crypto_browser_matrix.py`, which:**

- Parameterizes Playwright across Chromium, Firefox, and WebKit using the
  shared `browser_matrix` fixture in `tests/conftest.py`.
- Exercises the documented encryption runner (`tests/crypto_runner.html`) in
  each browser engine to prove Python-encrypted payloads decrypt correctly in
  JavaScript.
- Skips gracefully if a browser runtime is unavailable locally while still
  providing coverage wherever Playwright installs are present.

## 7. Test Coverage Improvements

Add specific tests for modules with lower coverage:

```bash
# Generate a coverage report
python -m pytest --cov=. --cov-report=term-missing

# Then add tests for the modules with lower coverage percentages
```

- ✅ Added regression coverage for `utils.system.resource_monitor.collect_resource_usage`
  to ensure CPU/memory metrics degrade gracefully when `psutil` raises errors.
- ✅ Added unit tests for `api.security.ensure_operator_access` to cover whitespace-tolerant
  operator tokens and Bearer authorization fallbacks.
- ✅ Added focused coverage for `utils.vision.image_analysis` to exercise whitespace-tolerant
  base64 decoding and defensive dimension parsing across PNG, GIF, and JPEG helpers.
- ✅ Added regression coverage for `utils.networking.relay_client._compose_relay_url` to ensure
  IPv6 relay endpoints retain their brackets when ports are injected.

## ✅ 8. Snapshot Testing (IMPLEMENTED)

**IMPLEMENTED in tests/unit/test_encrypt_snapshot.py, which includes:**
- Deterministic fixtures for AES key and IV generation to produce stable ciphertext snapshots.
- A saved JSON snapshot at `tests/unit/snapshots/encrypt_default_payload.json` capturing the
  canonical encryption output structure.
- Assertions that decrypted ciphertext matches the original payload while ensuring snapshot
  parity for ciphertext and metadata lengths.

## ✅ 9. Negative Testing (IMPLEMENTED)

**IMPLEMENTED in tests/unit/test_encrypt_input_validation.py and updated tests/test_crypto_failures.py, which include:**
- Validation that `encrypt.decrypt` raises helpful `ValueError` messages when required fields are
  missing.
- Type checks that reject non-mapping ciphertext payloads and non-bytes values before decrypting.
- Regression coverage to ensure missing-field scenarios now surface explicit exceptions rather than
  returning `None` silently.

## ✅ 10. Mock Server for JavaScript Tests (IMPLEMENTED)

**IMPLEMENTED in `tests/mock_js_server.js` with coverage in
`tests/test_js_mock_server.js`, which:**

- Spins up a lightweight HTTP server that mimics the encrypted `/api/v1/chat/completions`
  contract entirely in Node.js, avoiding the need to boot the Python stack for JavaScript
  tests.
- Handles RSA key exchange plus AES-CBC encryption/decryption using the same JSEncrypt and
  CryptoJS libraries as the browser client, ensuring wire compatibility.
- Responds with deterministic assistant messages so tests can assert on decrypted content.
- Is exercised automatically via `npm run test:js`, alongside the existing crypto unit tests.

## ✅ 11. Real-World Integration Testing with DSPACE

**IMPLEMENTED incrementally via** `tests/integration/test_dspace_chat_alias.py`, which now includes:

- `test_dspace_can_request_gpt5_alias` to ensure the compatibility alias resolves correctly.
- `test_dspace_receives_usage_metrics` to assert that chat completion responses surface non-negative
  token usage counters required by DSPACE's UI telemetry.
- ✅ `test_dspace_metadata_round_trip` ensures chat completions echo request metadata so DSPACE
  can correlate responses with active conversations.

These tests run automatically inside `run_all_tests.sh`, exercising the mock relay path that DSPACE
uses in production and giving us confidence that token.place remains a drop-in replacement for
OpenAI's chat API.

### Future expansion

Implement deeper integration tests with the [DSPACE project](https://github.com/democratizedspace/dspace) to verify token.place works as a drop-in replacement for OpenAI's API:

- ✅ Added a TypeScript `TokenPlaceClient` harness (`clients/token_place_client.ts`) exercised by
  `tests/test_token_place_client.ts`. The test starts the mock JS server, hits the OpenAI-compatible
  `/v1` alias, and proves encrypted chat completions decrypt correctly. This gives us confidence
  that DSPACE's browser bundler can rely on the documented client contract before wiring up the
  full app.
- ✅ Exercised the official OpenAI JavaScript SDK via `tests/integration/test_dspace_openai_sdk.py`
  and `tests/test_openai_js_sdk.ts`, booting the relay in mock mode and verifying the SDK can
  call token.place's `/v1` alias without code changes.

### Setup:

```bash
# Create a dedicated test directory
mkdir -p integration_tests/dspace
cd integration_tests

# Clone both projects as siblings
git clone https://github.com/futuroptimist/token.place.git
git clone https://github.com/democratizedspace/dspace.git -b v3
```

### Implementation steps:

1. ✅ **Create NPM Client Package (IMPLEMENTED 2025-10-31)**

   - Added `clients/package.json` describing the private `@tokenplace/client` bundle with a CommonJS
     entrypoint and generated type declarations.
   - Created `clients/index.ts` so the package re-exports `TokenPlaceClient` plus supporting types and
     supplies a default export for convenience.
   - Introduced `npm run build:client`, which compiles the TypeScript sources into `clients/dist/`
     using `tsconfig.client.json`.
   - Extended the JavaScript test suite with
     `tests/test_token_place_client_package.ts`, which builds the package, loads it via `require('../clients')`,
     and proves the exported client can complete an encrypted chat round-trip against the mock server.

2. ✅ **Create Test Configuration Script**

   - Added `integration_tests/setup.js`, which exports `startTokenPlace`, `startDspace`,
     and `cleanup` helpers plus shared port accessors so end-to-end harnesses can boot the
     mocked token.place stack alongside a DSPACE checkout without hand-written scripts.
   - The module rewrites `dspace/src/lib/openai.js` to import the published
     `TokenPlaceClient`, backs up and restores the original source during cleanup, and
     exposes dependency-injection hooks so tests can stub `child_process.spawn`.
   - Paired with the new regression `tests/test_integration_setup.ts`, which proves the
     helpers spawn the expected commands, patch the DSPACE client, and reliably restore the
     backup during teardown.
3. ✅ **Create Integration Test**

   - Added `tests/integration/test_dspace_browser_stub.py`, a Playwright-driven
     regression that boots the relay in mock mode, opens a lightweight
     DSPACE-style chat stub, and verifies encrypted chat requests complete
     successfully.
   - The accompanying HTML harness lives at
     `static/tests/dspace_integration_stub.html` so the relay can serve it from
     the same origin, avoiding CORS issues while mimicking the DSPACE UI flow.
   - The test asserts that the assistant reply propagates the mocked "Paris is
     the capital of France" message and that request metadata is echoed back to
     the browser, matching DSPACE's expectations.

4. ✅ **Create a Comprehensive Shell Script (IMPLEMENTED 2025-11-07)**

   - Added `integration_tests/run_dspace_integration.sh`, which automates cloning token.place and
     the DSPACE app, provisioning a Python virtual environment, and installing Node dependencies.
   - The helper bootstraps a sibling `token.place-client` package and, when a
   `test_dspace_integration.js` file is present, runs it through `npx mocha` to validate the
    round-trip flow.
   - Passing `--dry-run` prints every step without touching the network, enabling the accompanying
     pytest regression to verify that the workflow stays in sync with the documented plan.
   - A lightweight `integration_tests/run_integration_test.sh` wrapper keeps `run_all_tests.sh`
     green by default while still allowing maintainers to export
     `RUN_DSPACE_INTEGRATION=1` to run the full harness locally.

### Benefits:

- Tests token.place with a real-world application
- Validates API compatibility with OpenAI
- Tests end-to-end encryption in a practical scenario
- Helps maintain compatibility between projects as they evolve

### Implementation Notes:

1. ✅ Created a dedicated npm package template for token.place that mimics the OpenAI API interface
2. ✅ Use custom ports to avoid conflicts with development environments —
   `integration_tests/setup.js` now selects free ports automatically when the
   defaults are occupied, with regression coverage in
   `tests/test_integration_setup.ts`.
3. Automate the test setup to make it reproducible
4. The test should validate both successful encryption/decryption and proper API compatibility

## Implementation Priority

1. ✅ End-to-End Tests - To ensure the full workflow functions correctly
   Expanded via `tests/test_e2e_conversation_flow.py::test_openai_alias_end_to_end_flow` to cover
   the `/v1` OpenAI alias
2. ✅ Performance Benchmarks - To identify performance bottlenecks
3. ✅ Failure and Recovery Testing - To ensure the system is robust
4. ✅ Parameterized Tests - To verify functionality across different configurations
5. ✅ Security Tests - To identify potential vulnerabilities
6. ✅ Real-World Integration Testing - To validate practical usability and API compatibility

## Notes for Implementation

- ✅ **Integrate with CI/CD to run tests automatically** (2025-11-11)
  - Added `RUN_STRESS_TESTS` environment variable to both `run_all_tests.sh` and `run_all_tests.ps1` scripts
  - Stress tests (`tests/test_stress_streaming.py`) and performance benchmarks
    (`tests/test_performance_benchmarks.py`) now run conditionally when `RUN_STRESS_TESTS=1` is set
  - Updated `docs/TESTING.md` with instructions on enabling stress tests
  - Tests remain opt-in by default to keep CI build times reasonable
- ✅ Containerized test runner covers browser/integration suites via
  `scripts/run_tests_in_container.py`; the Docker image preinstalls Playwright Chromium so
  CI-equivalent runs work offline.
- ✅ Implement stress tests for production readiness
  - Added `tests/test_stress_streaming.py::test_stream_encryption_stress_handles_high_iteration_volume`
    exercising the streaming encryption helpers through 64 sequential chunk encrypt/decrypt cycles.
  - Introduced `utils/testing/stress.py::run_stream_encryption_stress_test` so maintainers can
    reuse the stress harness when tuning performance thresholds.
- ✅ Documented each pytest marker in `docs/TESTING.md`, adding run commands for every
  test category (`unit`, `api`, `crypto`, `browser`, `js`, `slow`, `benchmark`,
  `failure`, `parametrize`, `visual`, `e2e`, `real_llm`, and `security`).
