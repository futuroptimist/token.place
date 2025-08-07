# Testing Improvement Ideas

This document serves as a scratch pad for potential testing improvements to implement in the token.place project.

## 1. End-to-End Tests

Create full workflow tests that trace a request from client encryption through API transmission to server decryption and back:

```python
def test_complete_encrypted_conversation_flow():
    # Set up client keys
    client = ClientSimulator()
    # Get server public key
    server_key = client.fetch_server_public_key()
    # Encrypt a message
    encrypted_request = client.encrypt_message("Hello, secure world!", server_key)
    # Send to server
    response = client.send_request(encrypted_request)
    # Decrypt response
    decrypted_response = client.decrypt_response(response)
    # Verify expected flow completed correctly
    assert "Hello" in decrypted_response
```

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

## 6. Cross-Platform Browser Tests

Expand browser testing to include different environments:

```python
@pytest.mark.parametrize("browser_type", ["chromium", "firefox", "webkit"])
def test_js_encryption_in_different_browsers(browser_type, playwright):
    browser = getattr(playwright, browser_type).launch()
    page = browser.new_page()
    # Run your encryption tests in this browser
```

## 7. Test Coverage Improvements

Add specific tests for modules with lower coverage:

```bash
# Generate a coverage report
python -m pytest --cov=. --cov-report=term-missing

# Then add tests for the modules with lower coverage percentages
```

## 8. Snapshot Testing

Implement snapshot testing for stable parts of your crypto implementation:

```python
def test_encryption_output_format_consistency(snapshot):
    # Encrypt with a fixed key and seed for deterministic output
    result = encrypt_with_fixed_parameters("test")
    # Compare with saved snapshot
    snapshot.assert_match(json.dumps(result, sort_keys=True), "encryption_output.json")
```

## 9. Negative Testing

Add more explicit negative tests to verify proper error handling:

```python
def test_decrypt_with_missing_fields():
    # Test with missing IV
    bad_data = {'ciphertext': base64.b64encode(b'data').decode()}
    with pytest.raises(ValueError, match="Missing required field: iv"):
        decrypt_message(bad_data, private_key)
```

## 10. Mock Server for JavaScript Tests

Create a simple mock server to test the JavaScript client without relying on the full Python server:

```javascript
// mock_server.js
const express = require('express');
const app = express();
app.post('/api/chat', (req, res) => {
  // Return mock encrypted responses
});
// Then use this in js tests
```

## 11. Real-World Integration Testing with DSPACE

Implement integration tests with the [DSPACE project](https://github.com/democratizedspace/dspace) to verify token.place works as a drop-in replacement for OpenAI's API:

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
     pip install -r requirements.txt
     pip install -r requirements-dev.txt
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

1. End-to-End Tests - To ensure the full workflow functions correctly
2. ✅ Performance Benchmarks - To identify performance bottlenecks
3. ✅ Failure and Recovery Testing - To ensure the system is robust
4. ✅ Parameterized Tests - To verify functionality across different configurations
5. ✅ Security Tests - To identify potential vulnerabilities
6. Real-World Integration Testing - To validate practical usability and API compatibility

## Notes for Implementation

- Integrate with CI/CD to run tests automatically
- Consider using docker containers for browser and integration tests
- Implement stress tests for production readiness
- Add documentation for each test type
