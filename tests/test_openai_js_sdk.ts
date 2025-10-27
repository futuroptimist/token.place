import assert from 'node:assert';
import process from 'node:process';
import { Configuration, OpenAIApi } from 'openai';

async function run(): Promise<void> {
  const baseUrl = process.env.TOKEN_PLACE_BASE_URL ?? 'http://localhost:5056/v1';
  const apiKey = process.env.TOKEN_PLACE_API_KEY ?? 'test';
  const model = process.env.TOKEN_PLACE_MODEL ?? 'gpt-5-chat-latest';

  const configuration = new Configuration({
    apiKey,
    basePath: baseUrl,
  });

  const client = new OpenAIApi(configuration);

  const response = await client.createChatCompletion({
    model,
    messages: [
      { role: 'system', content: 'You are a cheerful assistant for DSPACE players.' },
      { role: 'user', content: 'Say hello from the OpenAI JavaScript SDK integration test.' },
    ],
  });

  const choices = response.data.choices ?? [];
  assert.ok(choices.length > 0, 'Expected at least one choice from the OpenAI SDK response');

  const message = choices[0]?.message;
  assert.ok(message, 'Missing message payload from OpenAI SDK response');
  assert.strictEqual(message?.role, 'assistant');
  assert.ok(
    typeof message?.content === 'string' && message.content.trim().length > 0,
    'Assistant message content should be a non-empty string',
  );

  console.log('✅ OpenAI JavaScript SDK integration test passed');
}

if (require.main === module) {
  run().catch((error) => {
    console.error('❌ OpenAI JavaScript SDK integration test failed:', error);
    process.exitCode = 1;
  });
}

export { run };
