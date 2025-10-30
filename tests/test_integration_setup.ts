import assert from 'node:assert';
import fs from 'node:fs';
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

async function runIntegrationSetupTest(): Promise<void> {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'token-place-integration-'));
  const integrationRoot = path.join(tempDir, 'integration_tests');
  const tokenPlaceRoot = path.join(integrationRoot, 'token.place');
  const dspaceRoot = path.join(integrationRoot, 'dspace');
  const dspaceLibDir = path.join(dspaceRoot, 'src', 'lib');
  fs.mkdirSync(dspaceLibDir, { recursive: true });
  fs.mkdirSync(tokenPlaceRoot, { recursive: true });

  const openaiPath = path.join(dspaceLibDir, 'openai.js');
  const originalOpenAiSource = "export default function originalOpenAI() { return 'openai'; }\n";
  fs.writeFileSync(openaiPath, originalOpenAiSource, 'utf8');

  const { fn: spawnStub, calls } = createSpawnStub();

  process.env.TOKEN_PLACE_INTEGRATION_ROOT = integrationRoot;

  const setup = await import('../integration_tests/setup.js');

  assert.strictEqual(setup.TOKEN_PLACE_PORT, 5555, 'Expected default token.place port');
  assert.strictEqual(setup.DSPACE_PORT, 4444, 'Expected default DSPACE port');

  const tokenPlaceProcess = await setup.startTokenPlace({
    spawn: spawnStub,
    projectRoot: tokenPlaceRoot
  });

  const dspaceProcess = await setup.startDspace({
    spawn: spawnStub,
    dspaceRoot
  });

  assert.strictEqual(calls.length, 2, 'Expected two spawn calls');

  const tokenPlaceCall = calls[0];
  assert.strictEqual(tokenPlaceCall.command, 'python');
  assert.deepStrictEqual(tokenPlaceCall.args, ['server.py', '--port=5555']);
  assert.ok(tokenPlaceCall.options);
  assert.strictEqual(tokenPlaceCall.options?.cwd, tokenPlaceRoot);
  assert.strictEqual(tokenPlaceCall.options?.env?.USE_MOCK_LLM, '1');

  const dspaceCall = calls[1];
  assert.strictEqual(dspaceCall.command, 'npm');
  assert.deepStrictEqual(dspaceCall.args, ['run', 'dev', '--', '--port=4444']);
  assert.strictEqual(dspaceCall.options?.cwd, dspaceRoot);

  const backupPath = `${openaiPath}.bak`;
  assert.ok(fs.existsSync(backupPath), 'Expected OpenAI file to be backed up');
  const rewrittenSource = fs.readFileSync(openaiPath, 'utf8');
  assert.ok(
    rewrittenSource.includes('TokenPlaceClient'),
    'Expected rewritten OpenAI client to reference TokenPlaceClient'
  );

  await setup.cleanup([tokenPlaceProcess, dspaceProcess]);

  assert.ok((tokenPlaceCall.process as StubChildProcess).killed, 'token.place process should be terminated');
  assert.ok((dspaceCall.process as StubChildProcess).killed, 'DSPACE process should be terminated');

  const restoredSource = fs.readFileSync(openaiPath, 'utf8');
  assert.strictEqual(restoredSource, originalOpenAiSource, 'OpenAI file should be restored after cleanup');
  assert.ok(!fs.existsSync(backupPath), 'Backup file should be removed during cleanup');

  fs.rmSync(tempDir, { recursive: true, force: true });
}

if (require.main === module) {
  runIntegrationSetupTest().catch(error => {
    console.error('❌ integration_tests/setup.js verification failed:', error);
    process.exitCode = 1;
  });
}

export { runIntegrationSetupTest };
