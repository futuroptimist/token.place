import assert from 'node:assert';
import { TokenPlaceClient } from '../clients/token_place_client';
import { startMockServer } from './mock_js_server';

type MockServer = Awaited<ReturnType<typeof startMockServer>>;

async function runTokenPlaceClientTest(): Promise<void> {
  const server: MockServer = await startMockServer();

  try {
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
        { role: 'user', content: 'Hello from TypeScript!' }
      ]
    });

    assert.ok(completion.choices.length > 0, 'Should return at least one choice');
    const message = completion.choices[0].message;
    assert.strictEqual(message.role, 'assistant');
    assert.strictEqual(
      message.content,
      'Mock response: Hello from TypeScript!'
    );

    console.log('✅ TokenPlaceClient TypeScript integration test passed');
  } finally {
    await server.stop();
  }
}

if (require.main === module) {
  runTokenPlaceClientTest().catch(error => {
    console.error('❌ TokenPlaceClient test failed:', error);
    process.exitCode = 1;
  });
}

export { runTokenPlaceClientTest };
