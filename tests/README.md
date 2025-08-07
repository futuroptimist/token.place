# token.place Testing Guide

This document provides an overview of the testing approach for token.place and instructions for running various test types.

## Test Types

### 1. Unit Tests
- Location: `tests/unit/`
- Purpose: Test individual components in isolation
- Command: `python -m pytest tests/unit/`

### 2. Integration Tests
- Location: `tests/integration/`
- Purpose: Test interactions between components
- Command: `python -m pytest tests/integration/`

### 3. API Tests
- Location: `tests/test_api.py`
- Purpose: Verify API functionality and compatibility
- Command: `python -m pytest tests/test_api.py`

### 4. Crypto Compatibility Tests
- Location: `tests/test_crypto_compatibility*.py`
- Purpose: Test cross-language encryption/decryption compatibility
- Commands:
  - Simple tests: `python tests/test_crypto_compatibility_simple.py`
  - Local tests: `python tests/test_crypto_compatibility_local.py`
  - Playwright tests: `python -m pytest tests/test_crypto_compatibility_playwright.py`

### 5. End-to-End Tests
- Location: `tests/test_e2e_*.py`
- Purpose: Test complete workflows
- Command: `python -m pytest tests/test_e2e_*.py`
- Additional Playwright test `tests/e2e/test_installation_docs.py` verifies that
  installation instructions exist in the README.

### 6. Failure Recovery Tests
- Location: `tests/test_failure_recovery.py`
- Purpose: Test system resilience against errors
- Command: `python -m pytest tests/test_failure_recovery.py`

### 7. JavaScript Tests
- Location: `tests/test_js_*.js`
- Purpose: Test JavaScript functionality
- Command: `npm run test:js`

### 8. DSPACE Integration Tests
- Location: `integration_tests/`
- Purpose: Test integration with DSPACE as a drop-in replacement for OpenAI
- Commands:
  - Full test suite: `cd integration_tests; .\run_integration_test.ps1`
  - Direct API test: `cd integration_tests; node dspace_tokenplace_test.mjs`

## Running All Tests

To run all tests in one command, use:

```bash
# Windows
.\run_all_tests.ps1

# Linux/macOS
./run_all_tests.sh
```

**Important Note:** The `run_all_tests` scripts include ALL test types above, including:
- Unit tests
- Integration tests
- API tests
- Crypto compatibility tests (simple, local, and Playwright)
- End-to-End tests
- Failure recovery tests
- JavaScript tests
- DSPACE integration tests

Some tests (particularly Playwright tests) require additional setup or running servers. The test scripts will handle this automatically, but you may need to ensure all prerequisites are installed.

## Prerequisites for All Tests

1. Python 3.11 or higher
2. Node.js 18 or higher
3. Playwright browsers installed (if running Playwright tests)
   ```bash
   python -m playwright install
   ```
4. All Python dependencies installed:
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt
   ```
5. All Node.js dependencies installed:
   ```bash
    npm ci
   ```

## Test Configuration

Tests can be configured using environment variables or by modifying `pytest.ini`. Common configuration options:

- `TEST_SERVER_PORT`: Port to use for test servers (default: 5000)
- `TEST_TIMEOUT`: Timeout in seconds for tests (default: 30)
- `TEST_COVERAGE`: Whether to collect coverage data (default: False)

## Writing New Tests

When writing new tests:

1. Place the test in the appropriate directory based on its type
2. Follow the existing naming conventions
3. Use fixtures where appropriate to avoid code duplication
4. Document any special setup requirements in the test file docstring

## Troubleshooting

- If Playwright tests fail, ensure browsers are installed with `python -m playwright install`
- For DSPACE integration test failures, check that ports 5555, 5556, and 4444 are available
- If JavaScript tests fail, ensure Node.js dependencies are installed and up to date

## Test Results and Expected Behavior

### Unit Tests
All unit tests in the `tests/unit/` directory should pass. These tests verify the core crypto functionality, model management, and relay client operations.

### Platform Tests
All platform tests in `tests/platform_tests/` should pass. These tests verify platform-specific behavior such as file paths and configuration loading.

### Integration Tests
All integration tests in `tests/integration/` should pass. These tests verify interactions between multiple components of the system.

### API Tests
All API tests in `tests/test_api.py` should pass. These tests verify that the API endpoints behave as expected.

### JavaScript Tests
All JavaScript crypto tests in `tests/test_js_crypto.js` should pass. These verify that the JavaScript implementation can encrypt and decrypt data correctly in a Node.js environment.

### Crypto Compatibility Tests
These tests may be skipped with "Server failed to register" messages in some environments, which is expected when testing in isolation. In a full deployment with all services running, these tests should pass.

The local compatibility tests (`test_crypto_compatibility_local.py`) should work in any environment with Node.js installed, as they don't require a browser.

### DSPACE Integration Tests
These tests require Chrome browser and may take some time to run as they start multiple servers. They verify that token.place can be used as a drop-in replacement for the OpenAI API in real-world applications.

## Current State of the Project

The project implements a secure token and AI service proxy with end-to-end encryption:

1. **Core Functionality**: All core functionality, including the crypto implementation, server setup, and API endpoints, is working correctly.

2. **Cross-Platform Support**: The platform tests confirm the project works across different operating systems.

3. **Encryption**: The encryption libraries have been thoroughly tested on both Python (backend) and JavaScript (client) sides.

4. **API Compatibility**: The project offers an OpenAI-compatible API interface that allows it to be used as a drop-in replacement in existing applications.

5. **Improvements**:
   - The JavaScript crypto code now runs in both browser and Node.js environments
   - We've established tests for both environments
   - The Python-JavaScript crypto compatibility is verified
   - Real-world application integration is tested with DSPACE

## Test Coverage

To generate a test coverage report:
```
python -m pytest --cov=. --cov-report=html
```

The HTML report will be available in the `htmlcov` directory.
