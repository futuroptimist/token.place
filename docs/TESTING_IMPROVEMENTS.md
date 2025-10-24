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

1. **Create NPM Client Package**:
   ```javascript
   // token.place-client/index.js

   class TokenPlaceClient {
     constructor(config = {}) {
      // token.place exposes both `/api/v1` and `/v1` for OpenAI compatibility
      this.baseUrl = config.baseUrl || 'http://localhost:5000/v1';
       this.clientKeys = null;
       this.serverPublicKey = null;
     }

     async initialize() {
       // Generate client keys
       this.clientKeys = await window.crypto.subtle.generateKey(
         { name: 'RSA-OAEP', modulusLength: 2048, ...keyParams },
         true,
         ['encrypt', 'decrypt']
       );

       // Fetch server public key
       const response = await fetch(`${this.baseUrl}/public-key`);
       const data = await response.json();
       this.serverPublicKey = data.public_key;

       return true;
     }

     // Implement OpenAI-compatible methods
     async createChatCompletion(params) {
       // Encrypt messages if encryption is enabled
       const encryptedParams = await this.encryptParams(params);

       // Send to token.place server
       const response = await fetch(`${this.baseUrl}/chat/completions`, {
         method: 'POST',
         headers: { 'Content-Type': 'application/json' },
         body: JSON.stringify(encryptedParams)
       });

       // Handle response (decrypt if necessary)
       const data = await response.json();
       return this.encrypted ? await this.decryptResponse(data) : data;
     }

     // Additional methods as needed
   }

   module.exports = TokenPlaceClient;
   ```

2. **Create Test Configuration Script**:
   ```javascript
   // integration_tests/setup.js

   const { spawn } = require('child_process');
   const path = require('path');
   const fs = require('fs');

   // Custom ports to avoid conflicts
   const TOKEN_PLACE_PORT = 5555;
   const DSPACE_PORT = 4444;

   async function startTokenPlace() {
     console.log('Starting token.place server...');
     const tokenPlaceServer = spawn('python', ['server.py', `--port=${TOKEN_PLACE_PORT}`], {
       cwd: path.join(__dirname, 'token.place'),
       env: { ...process.env, USE_MOCK_LLM: '1' },
     });

     // Log output
     tokenPlaceServer.stdout.on('data', (data) => console.log(`token.place: ${data}`));
     tokenPlaceServer.stderr.on('data', (data) => console.error(`token.place error: ${data}`));

     return tokenPlaceServer;
   }

   async function startDspace() {
     console.log('Starting DSPACE app...');

     // Replace OpenAI client with token.place client
     const openaiPath = path.join(__dirname, 'dspace/src/lib/openai.js');
     const openaiContent = fs.readFileSync(openaiPath, 'utf8');

     // Backup original file
     fs.writeFileSync(`${openaiPath}.bak`, openaiContent);

     // Replace with token.place client
     const tokenPlaceCode = `
       import TokenPlaceClient from '../../../token.place-client';

      const client = new TokenPlaceClient({
        // use /v1 so the OpenAI client works with token.place directly
        baseUrl: 'http://localhost:${TOKEN_PLACE_PORT}/v1',
        // Add any other configuration options
      });

       // Initialize the client
       await client.initialize();

       export default client;
     `;

     fs.writeFileSync(openaiPath, tokenPlaceCode);

     // Start DSPACE
     const dspaceServer = spawn('npm', ['run', 'dev', '--', `--port=${DSPACE_PORT}`], {
       cwd: path.join(__dirname, 'dspace'),
     });

     // Log output
     dspaceServer.stdout.on('data', (data) => console.log(`DSPACE: ${data}`));
     dspaceServer.stderr.on('data', (data) => console.error(`DSPACE error: ${data}`));

     return dspaceServer;
   }

   // Cleanup function
   function cleanup(servers) {
     servers.forEach(server => {
       if (server && server.pid) {
         process.kill(server.pid);
       }
     });

     // Restore original OpenAI file
     const openaiPath = path.join(__dirname, 'dspace/src/lib/openai.js');
     if (fs.existsSync(`${openaiPath}.bak`)) {
       fs.copyFileSync(`${openaiPath}.bak`, openaiPath);
       fs.unlinkSync(`${openaiPath}.bak`);
     }
   }

   // Export setup and teardown functions
   module.exports = {
     startTokenPlace,
     startDspace,
     cleanup,
     TOKEN_PLACE_PORT,
     DSPACE_PORT
   };
   ```

3. **Create Integration Test**:
   ```javascript
   // integration_tests/test_dspace_integration.js

   const { startTokenPlace, startDspace, cleanup, DSPACE_PORT } = require('./setup');
   const { Builder, By } = require('selenium-webdriver');
   const assert = require('assert');

   describe('DSPACE Integration Test', function() {
     this.timeout(60000); // Set longer timeout

     let tokenPlaceServer;
     let dspaceServer;
     let driver;

     before(async function() {
       // Start both servers
       tokenPlaceServer = await startTokenPlace();
       dspaceServer = await startDspace();

       // Wait for servers to initialize
       await new Promise(resolve => setTimeout(resolve, 10000));

       // Initialize Selenium WebDriver
       driver = await new Builder().forBrowser('chrome').build();
     });

     after(async function() {
       // Clean up
       if (driver) await driver.quit();
       cleanup([tokenPlaceServer, dspaceServer]);
     });

     it('should load DSPACE and send chat messages through token.place', async function() {
       // Navigate to DSPACE
       await driver.get(`http://localhost:${DSPACE_PORT}`);

       // Wait for page to load
       await driver.sleep(2000);

       // Find chat input and send a message
       const chatInput = await driver.findElement(By.id('chat-input'));
       await chatInput.sendKeys('Tell me about space exploration');
       await chatInput.submit();

       // Wait for response
       await driver.sleep(5000);

       // Verify response exists
       const chatMessages = await driver.findElements(By.className('chat-message'));
       assert(chatMessages.length >= 2, 'Expected at least a request and response message');

       // Verify response content
       const responseText = await chatMessages[chatMessages.length - 1].getText();
       assert(responseText.length > 0, 'Expected non-empty response');
       assert(responseText.toLowerCase().includes('space') ||
              responseText.toLowerCase().includes('exploration'),
              'Expected response to be relevant to the prompt');
     });
   });
   ```

4. **Create a Comprehensive Shell Script**:
   ```bash
   #!/bin/bash
   # integration_tests/run_dspace_integration.sh

   # Current directory
   DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

   # Check if directories exist, clone if not
   if [ ! -d "$DIR/token.place" ]; then
     echo "Cloning token.place repository..."
     git clone https://github.com/futuroptimist/token.place.git "$DIR/token.place"
   fi

   if [ ! -d "$DIR/dspace" ]; then
     echo "Cloning DSPACE repository..."
     git clone https://github.com/democratizedspace/dspace.git -b v3 "$DIR/dspace"
   fi

   # Setup Python environment for token.place
   cd "$DIR/token.place"
   if [ ! -d "env" ]; then
     echo "Setting up Python virtual environment..."
     python -m venv env
     source env/bin/activate
     pip install -r config/requirements_server.txt
     pip install -r config/requirements_relay.txt
     pip install -r requirements.txt
   else
     source env/bin/activate
   fi

   # Setup Node environment for DSPACE
   cd "$DIR/dspace"
   if [ ! -d "node_modules" ]; then
     echo "Installing DSPACE dependencies..."
      npm ci
   fi

   # Setup token.place client package
   cd "$DIR"
   if [ ! -d "token.place-client" ]; then
     echo "Creating token.place client package..."
     mkdir -p token.place-client
     # Create package.json
     echo '{
       "name": "token.place-client",
       "version": "0.1.0",
       "main": "index.js",
       "dependencies": {
         "node-fetch": "^2.6.7"
       }
     }' > token.place-client/package.json

     # Install dependencies
     cd token.place-client
      npm ci

     # Create the client library (implementation details in the JavaScript example above)
     echo "// token.place client implementation..." > index.js
   fi

   # Run the integration tests
   cd "$DIR"
   echo "Running integration tests..."
   mocha test_dspace_integration.js

   # Deactivate virtual environment
   deactivate

   echo "Integration tests completed."
   ```

### Benefits:

- Tests token.place with a real-world application
- Validates API compatibility with OpenAI
- Tests end-to-end encryption in a practical scenario
- Helps maintain compatibility between projects as they evolve

### Implementation Notes:

1. Consider creating a dedicated npm package for token.place that mimics the OpenAI API interface
2. Use custom ports to avoid conflicts with development environments
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

- Integrate with CI/CD to run tests automatically
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
