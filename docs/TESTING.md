# token.place Testing Guide

This guide provides an overview of the token.place testing approach, including test types, user journeys, and how to run various test suites.

## Prerequisites

Before running the tests, install the required Python and Node.js dependencies and download the Playwright browsers. The [AGENTS.md](AGENTS.md) file lists the exact commands, summarized here:

```bash
pip install -r config/requirements_server.txt
pip install -r config/requirements_relay.txt
pip install -r requirements.txt
npm ci
playwright install --with-deps chromium
```

The final step installs browser binaries and system dependencies so the Playwright-based tests can run successfully.

## Test Types

token.place uses several types of tests to ensure functionality, security, and performance:

### Unit Tests

Unit tests verify individual components in isolation. Run with:

```sh
python -m pytest tests/unit/
```

These tests cover numerous edge cases, including rejection of invalid PKCS#7 padding
such as zero-length padding or invalid block sizes, ensuring token.place's
cryptography remains robust.

### Integration Tests

Integration tests verify that components work together correctly:

```sh
python -m pytest tests/integration/
```

### End-to-End (E2E) Tests

E2E tests validate complete user workflows:

```sh
python -m pytest tests/test_e2e_*.py
```

### API Tests

API tests exercise the OpenAI-compatible HTTP routes to ensure request and
response formats stay stable:

```sh
python -m pytest -m api
```

This suite includes regressions such as `tests/test_api.py` and the
`tests/integration/test_openai_compatibility.py` flow that mirrors how client
libraries call `/v1`.

### Crypto Tests

Crypto tests validate the hybrid RSA/AES implementation and related helpers.
They cover key generation, encryption/decryption, and compatibility with the
JavaScript client:

```sh
python -m pytest -m crypto
```

Representative files include `tests/test_crypto_compatibility.py` and the
`tests/unit/test_encrypt_*` modules that guard against padding or key handling
regressions.

### JavaScript Tests

JavaScript tests confirm the browser-compatible encryption helpers behave the
same way in Node.js and Playwright environments:

```sh
npm run test:js
```

The script runs `tests/test_js_crypto.js` and `tests/test_js_mock_server.js` to
verify wire compatibility with the Python backend, while `pytest -m js` targets
Playwright-assisted coverage.

### Browser Tests

Browser tests launch Playwright to validate the interactive chat UI and in-page
encryption flows end-to-end:

```sh
python -m pytest -m browser
```

These tests are backed by fixtures in `tests/conftest.py` and cover scenarios in
`tests/test_e2e_chat.py` plus `tests/test_crypto_compatibility_playwright.py`.

### Slow Tests

Slow tests gate longer-running suites—such as GPU-heavy inference or
integration flows—so they only run when explicitly requested:

```sh
python -m pytest -m slow
```

This marker currently tags end-to-end encryption checks that spin up multiple
processes and the real LLM validation paths.

### Visual Verification Tests

Visual verification tests ensure UI consistency:

```sh
python -m pytest tests/visual_verification/ -m visual
```

### Real LLM Tests

Tests using actual LLM models:

```sh
python -m pytest tests/test_real_llm.py tests/test_real_llm_validation.py -v
```

### Security Tests

Security tests enforce cryptographic guardrails and static analysis expectations:

```sh
python -m pytest -m security
```

They execute suites such as `tests/test_security.py` and the automated Bandit
scan in `tests/test_security_bandit.py` to catch regressions before release.

## User Journeys and E2E Testing

token.place tests are organized around key user journeys that represent how users interact with the system:

### 1. End-to-End Encrypted Conversation

**User Journey**: A user sends an encrypted message through the relay to an LLM server and receives an encrypted response.

**Test Files**:
- `tests/test_e2e_conversation_flow.py` - Tests the complete conversation flow with encryption
- `tests/test_crypto_compatibility_local.py` - Verifies Python/JavaScript encryption compatibility
- `tests/test_e2e_network.py` - Tests network communication between components

**Key Scenarios Tested**:
- Single message exchange with encryption/decryption
- Multi-turn conversations with context maintenance
- Special character handling and message integrity

### 2. Browser-Based Chat Interface

**User Journey**: A user accesses the web interface, sends messages through the browser, and sees responses.

**Test Files**:
- `tests/test_e2e_chat.py` - Tests the browser-based chat functionality using Playwright
- `tests/test_crypto_compatibility_playwright.py` - Tests in-browser encryption

**Key Scenarios Tested**:
- Web interface loading and initialization
- Client-side encryption in the browser
- Multi-turn conversation in the browser UI

### 3. OpenAI-Compatible API Usage

**User Journey**: A developer uses the API with standard OpenAI client libraries.

The API routes are mirrored at `/v1` specifically so that the OpenAI Python
client can be used by simply setting `base_url` to `http://localhost:5055/v1`
during tests or `https://token.place/api/v1` in production.

**Test Files**:
- `tests/test_api.py` - Tests all API endpoints for functionality and compatibility
- `tests/integration/test_openai_compatibility.py` - Tests integration with OpenAI client libraries

**Key Scenarios Tested**:
- Model listing and information retrieval
- Chat completions with both encrypted and unencrypted modes
- Error handling and response formats

### 4. Cross-Platform Functionality

**User Journey**: Users run token.place on different operating systems.

**Test Files**:
- `tests/unit/test_path_handling.py` - Quick checks for path utilities on each OS
- `tests/platform_tests/test_path_handling.py` - Tests path handling across platforms
- `tests/platform_tests/test_config.py` - Tests configuration loading on different platforms

**Key Scenarios Tested**:
- Correct path resolution across Windows, macOS, and Linux
- Configuration loading from platform-specific locations
- Platform-specific behavior adaptations
- Support for XDG environment variables like `XDG_CONFIG_HOME` and `XDG_STATE_HOME`

### 5. Security and Failure Recovery

**User Journey**: System maintains security and recovers from errors during use.

**Test Files**:
- `tests/test_security.py` - Tests security properties of the encryption system
- `tests/test_crypto_failures.py` - Tests recovery from encryption failures
- `tests/test_failure_recovery.py` - Tests general system resilience

**Key Scenarios Tested**:
- Resilience against invalid keys or corrupted messages
- Proper error handling for network issues
- Security properties like forward secrecy and message integrity

### 6. Real LLM Integration

**User Journey**: A user interacts with a real LLM model through the system.

**Test Files**:
- `tests/test_real_llm.py` - Tests with actual LLM model
- `tests/test_real_llm_validation.py` - Advanced testing with real models

**Key Scenarios Tested**:
- Model download and verification
- Real inference with the LLM
- Complex reasoning tasks with the actual model

## Visual Verification Testing

The Visual Verification framework captures and compares screenshots of the UI to detect unwanted visual changes.

### How Visual Verification Works

1. The framework captures screenshots of key UI states
2. These screenshots are compared with baseline images
3. Differences are highlighted and quantified
4. A visual report is generated

### Running Visual Verification Tests

#### Creating Baselines

Before running comparison tests, you need to create baseline images:

```bash
# Windows
set CREATE_BASELINE=1
python -m pytest tests/visual_verification/test_chat_ui.py -m visual

# Unix/Linux/macOS
export CREATE_BASELINE=1
python -m pytest tests/visual_verification/test_chat_ui.py -m visual
```

#### Running Comparison Tests

Once baselines are created, you can run the tests to compare against the baselines:

```bash
python -m pytest tests/visual_verification/test_chat_ui.py -m visual
```

### Adding New Visual Tests

To add a new visual test:

1. Create a new test file in the `tests/visual_verification/` directory
2. Import the necessary utilities:
   ```python
   from .utils import capture_screenshot, save_as_baseline, compare_with_baseline
   ```
3. Use the `@pytest.mark.visual` decorator to mark your test functions
4. Use the test context fixtures:
   ```python
   def test_my_feature(page, visual_test_context, create_baseline_mode, setup_servers):
       # Test code here
   ```
5. Capture screenshots and compare with baselines or create new ones

## Test Coverage and Continuous Integration

token.place uses pytest-cov to track test coverage:

```sh
python -m pytest --cov=. --cov-report=html
```

This generates an HTML report in the `htmlcov/` directory.

## Test Markers

token.place uses pytest markers to categorize tests:

- `unit`: Unit tests
- `integration`: Integration tests
- `api`: API tests
- `crypto`: Encryption-related tests
- `js`: JavaScript tests
- `browser`: Tests requiring a browser
- `slow`: Slow-running tests
- `visual`: Visual verification tests
- `benchmark`: Performance benchmark tests
- `failure`: Failure recovery tests
- `e2e`: End-to-end tests
- `parametrize`: Parameterized tests

To run tests with a specific marker:

```sh
python -m pytest -m marker_name
```

## Specialized Test Suites

### Benchmark Tests

```sh
python -m pytest tests/test_performance_benchmarks.py
```

These benchmark tests measure encryption throughput across payload sizes and
track RSA/AES key generation costs to prevent performance regressions.

### Failure Recovery Tests

```sh
python -m pytest tests/test_failure_recovery.py
```

### Parameterized Tests

```sh
python -m pytest tests/test_parameterized.py
```

### Security Tests

```sh
python -m pytest tests/test_security.py
```

### Running all tests

To execute the complete test suite that CI runs, use:

```sh
npm run test:ci
```

This wraps `./run_all_tests.sh` and runs every available test.

## Additional Resources

- [TESTING_IMPROVEMENTS.md](TESTING_IMPROVEMENTS.md) - Detailed ideas for test improvements
- [tests/visual_verification/README.md](../tests/visual_verification/README.md) - Visual verification framework documentation
