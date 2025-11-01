import assert from 'node:assert';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import type { ChildProcess, SpawnOptions } from 'node:child_process';

interface StubChildProcess extends EventEmitter {
  pid: number;
  killed: boolean;
  stdout: EventEmitter;
  stderr: EventEmitter;
  kill: (signal?: NodeJS.Signals | number) => boolean;
}

type SpawnCall = {
  command: string;
  args: readonly string[];
  options?: SpawnOptions;
  process: StubChildProcess;
};

type SpawnImpl = (
  command: string,
  args?: readonly string[],
  options?: SpawnOptions
) => ChildProcess;

function createStubChildProcess(pid: number): StubChildProcess {
  const stdout = new EventEmitter();
  const stderr = new EventEmitter();
  const proc = new EventEmitter() as StubChildProcess;
  proc.pid = pid;
  proc.killed = false;
  proc.stdout = stdout;
  proc.stderr = stderr;
  proc.kill = () => {
    proc.killed = true;
    return true;
  };
  return proc;
}

function createSpawnStub(): { fn: SpawnImpl; calls: SpawnCall[] } {
  const calls: SpawnCall[] = [];
  let nextPid = 1000;

  const fn: SpawnImpl = (command, args = [], options) => {
    const proc = createStubChildProcess(nextPid++);
    calls.push({ command, args, options, process: proc });
    setImmediate(() => proc.emit('spawn'));
    return proc as unknown as ChildProcess;
  };

  return { fn, calls };
}

interface IntegrationSandbox {
  tempDir: string;
  integrationRoot: string;
  tokenPlaceRoot: string;
  dspaceRoot: string;
  openaiPath: string;
}

const ORIGINAL_OPENAI_SOURCE = "export default function originalOpenAI() { return 'openai'; }\n";

function createIntegrationSandbox(): IntegrationSandbox {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'token-place-integration-'));
  const integrationRoot = path.join(tempDir, 'integration_tests');
  const tokenPlaceRoot = path.join(integrationRoot, 'token.place');
  const dspaceRoot = path.join(integrationRoot, 'dspace');
  const dspaceLibDir = path.join(dspaceRoot, 'src', 'lib');
  fs.mkdirSync(dspaceLibDir, { recursive: true });
  fs.mkdirSync(tokenPlaceRoot, { recursive: true });

  const openaiPath = path.join(dspaceLibDir, 'openai.js');
  fs.writeFileSync(openaiPath, ORIGINAL_OPENAI_SOURCE, 'utf8');

  return { tempDir, integrationRoot, tokenPlaceRoot, dspaceRoot, openaiPath };
}

async function listenOn(port: number): Promise<net.Server> {
  return await new Promise<net.Server>((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => resolve(server));
  });
}

async function closeServer(server: net.Server): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.close(error => {
      if (error) {
        reject(error);
        return;
      }
      resolve();
    });
  });
}

async function runIntegrationSetupTest(): Promise<void> {
  const sandbox = createIntegrationSandbox();
  process.env.TOKEN_PLACE_INTEGRATION_ROOT = sandbox.integrationRoot;

  const setup = await import('../integration_tests/setup.js');

  assert.strictEqual(setup.DEFAULT_TOKEN_PLACE_PORT, 5555, 'Expected default token.place port');
  assert.strictEqual(setup.DEFAULT_DSPACE_PORT, 4444, 'Expected default DSPACE port');

const { fn: defaultSpawn, calls: defaultCalls } = createSpawnStub();

  const { process: tokenPlaceProcess, port: tokenPlacePort } = await setup.startTokenPlace({
    spawn: defaultSpawn,
    projectRoot: sandbox.tokenPlaceRoot
  });

  const { process: dspaceProcess, port: dspacePort } = await setup.startDspace({
    spawn: defaultSpawn,
    dspaceRoot: sandbox.dspaceRoot
  });

  assert.strictEqual(defaultCalls.length, 2, 'Expected two spawn calls for default startup');

  const tokenPlaceCall = defaultCalls[0];
  assert.strictEqual(tokenPlaceCall.command, 'python');
  assert.deepStrictEqual(tokenPlaceCall.args, ['server.py', '--port=5555']);
  assert.ok(tokenPlaceCall.options);
  assert.strictEqual(tokenPlaceCall.options?.cwd, sandbox.tokenPlaceRoot);
  assert.strictEqual(tokenPlaceCall.options?.env?.USE_MOCK_LLM, '1');
  assert.strictEqual(tokenPlacePort, setup.DEFAULT_TOKEN_PLACE_PORT, 'Expected default token.place port when free');
  assert.strictEqual(
    setup.TOKEN_PLACE_PORT,
    tokenPlacePort,
    'Exported token.place port should reflect the resolved port'
  );

  const dspaceCall = defaultCalls[1];
  assert.strictEqual(dspaceCall.command, 'npm');
  assert.deepStrictEqual(dspaceCall.args, ['run', 'dev', '--', '--port=4444']);
  assert.strictEqual(dspaceCall.options?.cwd, sandbox.dspaceRoot);
  assert.strictEqual(dspacePort, setup.DEFAULT_DSPACE_PORT, 'Expected default DSPACE port when free');
  assert.strictEqual(
    setup.DSPACE_PORT,
    dspacePort,
    'Exported DSPACE port should reflect the resolved port'
  );

  const backupPath = `${sandbox.openaiPath}.bak`;
  assert.ok(fs.existsSync(backupPath), 'Expected OpenAI file to be backed up');
  const rewrittenSource = fs.readFileSync(sandbox.openaiPath, 'utf8');
  assert.ok(
    rewrittenSource.includes('TokenPlaceClient'),
    'Expected rewritten OpenAI client to reference TokenPlaceClient'
  );

  await setup.cleanup([tokenPlaceProcess, dspaceProcess]);

  assert.ok((tokenPlaceCall.process as StubChildProcess).killed, 'token.place process should be terminated');
  assert.ok((dspaceCall.process as StubChildProcess).killed, 'DSPACE process should be terminated');

  assert.strictEqual(
    setup.TOKEN_PLACE_PORT,
    setup.DEFAULT_TOKEN_PLACE_PORT,
    'Token.place port export should reset after cleanup'
  );
  assert.strictEqual(
    setup.DSPACE_PORT,
    setup.DEFAULT_DSPACE_PORT,
    'DSPACE port export should reset after cleanup'
  );

  const restoredSource = fs.readFileSync(sandbox.openaiPath, 'utf8');
  assert.strictEqual(restoredSource, ORIGINAL_OPENAI_SOURCE, 'OpenAI file should be restored after cleanup');
  assert.ok(!fs.existsSync(backupPath), 'Backup file should be removed during cleanup');

  const tokenPlaceBlocker = await listenOn(setup.DEFAULT_TOKEN_PLACE_PORT);
  try {
    const { fn: fallbackSpawn, calls: fallbackCalls } = createSpawnStub();
    const { process: fallbackProcess, port: fallbackPort } = await setup.startTokenPlace({
      spawn: fallbackSpawn,
      projectRoot: sandbox.tokenPlaceRoot
    });

    assert.notStrictEqual(
      fallbackPort,
      setup.DEFAULT_TOKEN_PLACE_PORT,
      'Expected fallback to a new token.place port when default is busy'
    );

    assert.strictEqual(
      setup.TOKEN_PLACE_PORT,
      fallbackPort,
      'Token.place port export should update when fallback port is selected'
    );

    const fallbackCall = fallbackCalls[0];
    assert.deepStrictEqual(fallbackCall.args, ['server.py', `--port=${fallbackPort}`]);

    await setup.cleanup([fallbackProcess]);
  } finally {
    await closeServer(tokenPlaceBlocker);
  }

  const dspaceBlocker = await listenOn(setup.DEFAULT_DSPACE_PORT);
  try {
    const { fn: dspaceFallbackSpawn, calls: dspaceFallbackCalls } = createSpawnStub();
    const { process: fallbackDspaceProcess, port: fallbackDspacePort } = await setup.startDspace({
      spawn: dspaceFallbackSpawn,
      dspaceRoot: sandbox.dspaceRoot
    });

    assert.notStrictEqual(
      fallbackDspacePort,
      setup.DEFAULT_DSPACE_PORT,
      'Expected fallback to a new DSPACE port when default is busy'
    );

    assert.strictEqual(
      setup.DSPACE_PORT,
      fallbackDspacePort,
      'DSPACE port export should update when fallback port is selected'
    );

    const dspaceFallbackCall = dspaceFallbackCalls[0];
    assert.deepStrictEqual(dspaceFallbackCall.args, ['run', 'dev', '--', `--port=${fallbackDspacePort}`]);

    const rewrittenFallbackSource = fs.readFileSync(sandbox.openaiPath, 'utf8');
    assert.ok(
      rewrittenFallbackSource.includes(`http://localhost:${fallbackDspacePort}/v1`),
      'Rewritten OpenAI client should reference the fallback port'
    );

    await setup.cleanup([fallbackDspaceProcess]);
  } finally {
    await closeServer(dspaceBlocker);
  }

  fs.rmSync(sandbox.tempDir, { recursive: true, force: true });
}

if (require.main === module) {
  runIntegrationSetupTest().catch(error => {
    console.error('❌ integration_tests/setup.js verification failed:', error);
    process.exitCode = 1;
  });
}

export { runIntegrationSetupTest };
