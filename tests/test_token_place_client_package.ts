import assert from 'node:assert';
import { execFileSync } from 'node:child_process';
import path from 'node:path';

import { startMockServer } from './mock_js_server';

async function runTokenPlaceClientPackageTest(): Promise<void> {
  const projectRoot = path.resolve(__dirname, '..');
  execFileSync('npm', ['run', 'build:client'], {
    cwd: projectRoot,
    stdio: 'inherit'
  });

  const server = await startMockServer();

  try {
    const packageEntry = require(path.resolve(projectRoot, 'clients'));
    const TokenPlaceClient = packageEntry.TokenPlaceClient ?? packageEntry.default;

    assert.ok(
      typeof TokenPlaceClient === 'function',
      'TokenPlaceClient export should be available from the package entrypoint'
    );

    const client = new TokenPlaceClient({
      baseUrl: `${server.baseUrl}/v1`,
      publicKeyPath: 'public-key',
      chatCompletionsPath: 'chat/completions',
      model: 'mock-llm'
    });

    await client.initialize();

    const completion = await client.createChatCompletion({
      messages: [
        { role: 'system', content: 'You are a cheerful assistant.' },
        { role: 'user', content: 'Package smoke test!' }
      ]
    });

    assert.ok(completion.choices.length > 0, 'Should return at least one choice');

    const message = completion.choices[0].message;
    assert.strictEqual(message.role, 'assistant');
    assert.strictEqual(
      message.content,
      'Mock response: Package smoke test!'
    );

    console.log('✅ TokenPlaceClient package smoke test passed');
  } finally {
    await server.stop();
  }
}

if (require.main === module) {
  runTokenPlaceClientPackageTest().catch(error => {
    console.error('❌ TokenPlaceClient package smoke test failed:', error);
    process.exitCode = 1;
  });
}

export { runTokenPlaceClientPackageTest };
