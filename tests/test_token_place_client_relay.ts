import assert from 'node:assert';
import { spawn, ChildProcess } from 'node:child_process';
import { once } from 'node:events';
import path from 'node:path';
import process from 'node:process';
import { setTimeout as delay } from 'node:timers/promises';

import { TokenPlaceClient } from '../clients/token_place_client';

const RELAY_PORT = 5060;
const BASE_URL = `http://127.0.0.1:${RELAY_PORT}`;
const HEALTH_ENDPOINT = `${BASE_URL}/v1/health`;
const PROJECT_ROOT = path.resolve(__dirname, '..');
const PYTHON_EXECUTABLE = process.env.PYTHON ?? 'python';

async function waitForRelayReady(): Promise<void> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const response = await fetch(HEALTH_ENDPOINT, { method: 'GET' });
      if (response.ok) {
        console.log(`✓ Relay health check succeeded on attempt ${attempt + 1}`);
        return;
      }
      console.log(`✗ Health check attempt ${attempt + 1}: HTTP ${response.status}`);
    } catch (error) {
      if (attempt === 0 || attempt % 10 === 9) {
        console.log(`✗ Health check attempt ${attempt + 1}: ${error instanceof Error ? error.message : String(error)}`);
      }
    }

    await delay(1000);
  }

  throw new Error('Relay health check did not succeed within 30 seconds');
}

async function stopProcess(child: ChildProcess): Promise<void> {
  if (child.killed) {
    return;
  }

  child.kill('SIGTERM');

  try {
    let timeoutHandle: NodeJS.Timeout;
    const timeoutPromise = new Promise((_, reject) => {
      timeoutHandle = setTimeout(() => {
        reject(new Error('Process close timeout'));
      }, 5000);
    });
    await Promise.race([
      once(child, 'close').then(() => {
        clearTimeout(timeoutHandle);
      }),
      timeoutPromise,
    ]);
  } catch {
    child.kill('SIGKILL');
  }
}

async function runTokenPlaceRelayClientTest(): Promise<void> {
  const env = {
    ...process.env,
    USE_MOCK_LLM: '1',
  };

  const relay = spawn(PYTHON_EXECUTABLE, ['relay.py', '--port', String(RELAY_PORT)], {
    cwd: PROJECT_ROOT,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  const relayLogs: string[] = [];
  relay.stdout?.on('data', chunk => {
    relayLogs.push(chunk.toString());
  });
  relay.stderr?.on('data', chunk => {
    relayLogs.push(chunk.toString());
  });

  let relayExited = false;
  relay.on('exit', (code, signal) => {
    relayExited = true;
    relayLogs.push(`\n[Process exited with code ${code}, signal ${signal}]\n`);
  });

  try {
    await once(relay, 'spawn');
    await waitForRelayReady();

    if (relayExited) {
      throw new Error('Relay process exited unexpectedly during startup');
    }

    const client = new TokenPlaceClient({
      baseUrl: BASE_URL,
      publicKeyPath: 'v1/public-key',
      chatCompletionsPath: 'v1/chat/completions',
      model: 'gpt-5-chat-latest',
    });

    await client.initialize();

    const completion = await client.createChatCompletion({
      messages: [
        { role: 'system', content: 'You are a cheerful assistant.' },
        { role: 'user', content: 'What is the capital of France?' },
      ],
    });

    assert.ok(
      completion.choices.length > 0,
      'Encrypted relay response should include at least one choice',
    );

    const message = completion.choices[0]?.message;
    assert.ok(message, 'Relay response is missing assistant message content');
    assert.strictEqual(message.role, 'assistant');
    assert.ok(
      typeof message.content === 'string' && message.content.includes('Paris'),
      'Decrypted assistant reply should mention Paris to prove mock response surfaced',
    );

    console.log('✅ TokenPlaceClient relay integration test passed');
  } catch (caughtError) {
    const logOutput = relayLogs.join('');
    if (logOutput) {
      console.error('Relay stderr output:\n', logOutput);
    }
    throw caughtError;
  } finally {
    await stopProcess(relay);
  }
}

if (require.main === module) {
  runTokenPlaceRelayClientTest().catch(error => {
    console.error('❌ TokenPlaceClient relay integration test failed:', error);
    process.exitCode = 1;
  });
}

export { runTokenPlaceRelayClientTest };
