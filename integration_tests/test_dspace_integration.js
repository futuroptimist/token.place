/**
 * DSPACE Integration Test
 *
 * This test validates that token.place works as a drop-in replacement for OpenAI's API
 * in the DSPACE application. It exercises the full integration workflow.
 *
 * The test:
 * 1. Starts a token.place relay with mock LLM
 * 2. Validates API compatibility with OpenAI's chat completions format
 * 3. Tests unencrypted requests (encryption is tested in other test suites)
 * 4. Verifies metadata round-trip and usage metrics
 */

const assert = require('node:assert');
const path = require('node:path');
const { spawn } = require('node:child_process');
const net = require('node:net');

/**
 * Test metadata round-trip to verify DSPACE can correlate responses
 */
async function testMetadataRoundTrip(baseUrl) {
  const requestPayload = {
    model: 'gpt-5-chat-latest',
    messages: [
      { role: 'user', content: 'Test metadata' },
    ],
    metadata: {
      conversation_id: 'test-conv-123',
      user_id: 'test-user-456',
    },
  };

  const response = await fetch(`${baseUrl}/api/v1/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(requestPayload),
  });

  assert.ok(response.ok, 'Metadata request should succeed');

  const result = await response.json();

  // The response structure should be OpenAI-compatible
  assert.ok(result.choices, 'Response should have choices');
  assert.ok(result.id, 'Response should have an id');
  assert.ok(result.object, 'Response should have an object type');
  assert.deepStrictEqual(result.metadata, requestPayload.metadata, 'Response should echo request metadata');

  console.log('✅ Metadata round-trip test passed');

  return result;
}

/**
 * Test usage metrics that DSPACE's UI telemetry requires
 */
async function testUsageMetrics(baseUrl) {
  const response = await fetch(`${baseUrl}/api/v1/chat/completions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model: 'gpt-5-chat-latest',
      messages: [{ role: 'user', content: 'Hello' }],
    }),
  });

  assert.ok(response.ok, 'Usage metrics request should succeed');

  const result = await response.json();

  // OpenAI responses include usage statistics
  assert.ok(
    result.usage && typeof result.usage === 'object',
    'Usage payload missing from chat completion response'
  );
  assert.ok(
    typeof result.usage.prompt_tokens === 'number' && result.usage.prompt_tokens >= 0,
    'Should have non-negative prompt_tokens'
  );
  assert.ok(
    typeof result.usage.completion_tokens === 'number' && result.usage.completion_tokens >= 0,
    'Should have non-negative completion_tokens'
  );
  assert.ok(
    typeof result.usage.total_tokens === 'number' && result.usage.total_tokens >= 0,
    'Should have non-negative total_tokens'
  );

  console.log('✅ Usage metrics test passed');

  return result;
}

/**
 * Find an available port by letting the OS assign one
 */
async function findAvailablePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();

    server.once('error', reject);

    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : undefined;

      server.close(error => {
        if (error) {
          reject(error);
          return;
        }

        if (typeof port !== 'number') {
          reject(new Error('Unable to determine available port'));
          return;
        }

        resolve(port);
      });
    });
  });
}

/**
 * Start the relay server with mock LLM
 */
async function startRelayServer(port = 5555) {
  const projectRoot = path.join(__dirname, '..');
  const pythonExecutable = process.env.PYTHON_EXECUTABLE || 'python3';

  const env = {
    ...process.env,
    USE_MOCK_LLM: '1',
    TOKEN_PLACE_ENV: 'testing',
  };

  const relayProcess = spawn(pythonExecutable, ['relay.py', '--port', String(port)], {
    cwd: projectRoot,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  // Consume stdout to prevent buffer from filling up
  relayProcess.stdout.on('data', (data) => {
    const message = data.toString().trim();
    if (message) {
      // Optionally log, or just consume
      // console.log(`  relay stdout: ${message}`);
    }
  });

  // Log stderr for debugging
  relayProcess.stderr.on('data', (data) => {
    const message = data.toString().trim();
    if (message && !message.includes('UserWarning') && !message.includes('DeprecationWarning')) {
      console.log(`  relay: ${message}`);
    }
  });

  return { process: relayProcess, port };
}

/**
 * Wait for the server to be ready by polling the health endpoint
 */
async function waitForServerReady(baseUrl, maxAttempts = 30, delayMs = 1000) {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      // Use /v1/health which returns 200 even when relay is degraded (no upstream servers)
      // This is consistent with other integration tests that check API availability
      const response = await fetch(`${baseUrl}/v1/health`, {
        signal: AbortSignal.timeout(2000),
      });
      if (response.ok) {
        console.log(`✅ Server is ready after ${attempt} attempt(s)`);
        return;
      }
    } catch (error) {
      // Connection refused or timeout, keep trying
      if (attempt < maxAttempts) {
        await new Promise(resolve => setTimeout(resolve, delayMs));
      }
    }
  }
  throw new Error(`Server did not become ready after ${maxAttempts} attempts`);
}

/**
 * Test that the gpt-5-chat-latest alias works (DSPACE compatibility requirement)
 */
async function testModelAlias(baseUrl) {
  const response = await fetch(`${baseUrl}/api/v1/models`, {
    headers: { 'Content-Type': 'application/json' },
  });

  assert.ok(response.ok, 'Models endpoint should be accessible');

  const result = await response.json();
  assert.ok(result.data, 'Should return models data array');

  const hasGpt5Alias = result.data.some(model => model.id === 'gpt-5-chat-latest');

  // Note: In mock mode, gpt-5-chat-latest might not be listed but still works at chat completions
  if (hasGpt5Alias) {
    console.log('✅ Model alias test passed (alias found in models list)');
  } else {
    console.log('⚠️  gpt-5-chat-latest not in models list (this is OK in mock mode)');
    console.log('   Will verify it works at the chat completions endpoint instead');
  }

  return result;
}

/**
 * Main integration test orchestrator
 */
async function runDspaceIntegrationTest() {
  console.log('🚀 Starting DSPACE integration test...\n');

  let tokenPlaceProcess = null;

  try {
    // Start relay server with mock LLM on an available port
    console.log('Starting token.place relay...');
    const port = await findAvailablePort();
    const { process: relayProcess } = await startRelayServer(port);

    tokenPlaceProcess = relayProcess;
    const baseUrl = `http://localhost:${port}`;

    console.log(`✅ Relay started on port ${port}\n`);

    // Wait for server to be ready
    console.log('Waiting for server to be ready...');
    await waitForServerReady(baseUrl);
    console.log();

    // Test 1: Verify model alias exists
    console.log('Test 1: Model alias compatibility');
    await testModelAlias(baseUrl);
    console.log();

    // Test 2: Test chat completion (without encryption for simpler testing)
    console.log('Test 2: Chat completion with gpt-5-chat-latest alias');
    const chatResponse = await fetch(`${baseUrl}/api/v1/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'gpt-5-chat-latest',
        messages: [
          { role: 'system', content: 'You are a helpful assistant embedded in DSPACE.' },
          { role: 'user', content: 'What is the capital of France?' },
        ],
      }),
    });

    assert.ok(chatResponse.ok, 'Chat completion request should succeed');
    const chatResult = await chatResponse.json();

    assert.strictEqual(chatResult.model, 'gpt-5-chat-latest', 'Response should echo the model alias');
    assert.ok(chatResult.choices && chatResult.choices.length > 0, 'Should have at least one choice');
    assert.strictEqual(chatResult.choices[0].message.role, 'assistant', 'Message role should be assistant');
    assert.ok(
      typeof chatResult.choices[0].message.content === 'string' &&
      chatResult.choices[0].message.content.length > 0,
      'Message should have non-empty content'
    );

    console.log('✅ Chat completion test passed');
    console.log(`   Model: ${chatResult.model}`);
    console.log(`   Response: ${chatResult.choices[0].message.content.substring(0, 80)}...`);
    console.log();

    // Test 3: Test metadata round-trip
    console.log('Test 3: Metadata round-trip');
    await testMetadataRoundTrip(baseUrl);
    console.log();

    // Test 4: Test usage metrics
    console.log('Test 4: Usage metrics');
    await testUsageMetrics(baseUrl);
    console.log();

    console.log('✨ All DSPACE integration tests passed!\n');
  } catch (error) {
    console.error('❌ DSPACE integration test failed:', error);
    process.exitCode = 1;
    throw error;
  } finally {
    // Cleanup
    if (tokenPlaceProcess) {
      console.log('Cleaning up...');
      // Gracefully terminate the process, wait for exit, force kill if needed
      tokenPlaceProcess.kill('SIGTERM');
      const timeoutMs = 5000;
      const exited = await Promise.race([
        new Promise(resolve => tokenPlaceProcess.once('exit', () => resolve('exited'))),
        new Promise(resolve => setTimeout(resolve, timeoutMs, 'timeout')),
      ]);
      if (exited === 'timeout') {
        console.warn(`Process did not exit after ${timeoutMs}ms, sending SIGKILL...`);
        tokenPlaceProcess.kill('SIGKILL');
        await new Promise(resolve => tokenPlaceProcess.once('exit', resolve));
      }
      console.log('✅ Cleanup complete');
    }
  }
}

// Run the test if this file is executed directly
if (require.main === module) {
  runDspaceIntegrationTest().catch(error => {
    console.error('Test execution failed:', error);
    process.exit(1);
  });
}

module.exports = { runDspaceIntegrationTest };
