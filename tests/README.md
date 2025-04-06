# Test Documentation

This document outlines how to run the various tests for the token.place project.

## Prerequisites

- Python 3.11 or higher
- Node.js 18 or higher
- Python virtual environment (recommended)

## Setup

1. Create and activate a Python virtual environment:
   ```
   python -m venv env
   env\Scripts\activate  # Windows
   source env/bin/activate  # Linux/Mac
   ```

2. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Install Node.js dependencies:
   ```
   npm install
   ```

## Running Tests

### Python Unit Tests

Run all Python unit tests:
```
python -m pytest tests/unit/
```

Run specific unit tests:
```
python -m pytest tests/unit/test_crypto_manager.py -v
```

### Python Platform Tests

Run platform-specific tests:
```
python -m pytest tests/platform_tests/ -v
```

### Python Integration Tests

Run integration tests:
```
python -m pytest tests/integration/ -v
```

### API Tests

Run API endpoint tests:
```
python -m pytest tests/test_api.py -v
```

### JavaScript Tests

Run JavaScript crypto functionality tests:
```
npm run test:js
```

### Crypto Compatibility Tests

There are multiple ways to run the crypto compatibility tests:

#### 1. Using Local Browser-less Node.js Tests

For quick development testing without a browser:
```
python tests/test_crypto_compatibility_local.py
```

These tests use Node.js to execute the JavaScript crypto functions directly, without requiring a browser environment. This is useful for rapid development and debugging.

#### 2. Using Playwright with Automatic Web Server

The most complete and production-like testing method that uses real browsers via Playwright with an automatic web server:

```
python -m pytest tests/test_crypto_compatibility_playwright.py -v
```

This test automatically:
1. Starts a local web server
2. Launches a browser with Playwright
3. Runs the crypto compatibility tests between Python and browser JavaScript
4. Cleans up resources when finished

Prerequisites for Playwright tests:
- Install Playwright browser engines:
  ```
  python -m playwright install
  ```

#### 3. Using Playwright with Manual Web Server

For traditional manual server approach (original method):
```
# Terminal 1: Start a local server
python -m http.server 8000

# Terminal 2: Run the tests
python -m pytest tests/test_crypto_compatibility.py -v
```

### DSPACE Integration Tests

The project includes integration tests with the [DSPACE project](https://github.com/democratizedspace/dspace) to verify that token.place can be used as a drop-in replacement for OpenAI's API:

```bash
# On Unix/Linux/macOS
cd integration_tests
chmod +x run_integration_test.sh
./run_integration_test.sh

# On Windows
cd integration_tests
run_integration_test.bat
```

These tests:
1. Clone both token.place and DSPACE repositories
2. Set up a tokenplace-client that implements an OpenAI-compatible interface
3. Configure DSPACE to use token.place instead of OpenAI
4. Perform end-to-end API integration testing with a browser

For more details, see [integration_tests/README.md](../integration_tests/README.md).

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