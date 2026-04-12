import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { App } from './App';

const invokeMock = vi.fn();
const listenMock = vi.fn();
const eventHandlers = new Map<string, (evt: { payload: Record<string, unknown> }) => void>();

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

vi.mock('@tauri-apps/api/event', () => ({
  listen: (...args: unknown[]) => listenMock(...args),
}));

vi.mock('@tauri-apps/plugin-dialog', () => ({
  open: vi.fn(),
}));

const relayUrl = process.env.DESKTOP_OPERATOR_E2E_RELAY_URL ?? 'http://127.0.0.1:5010';
const shouldRun = process.env.RUN_DESKTOP_OPERATOR_E2E === '1';
const maybeIt = shouldRun ? it : it.skip;

let bridgeChild: ChildProcessWithoutNullStreams | null = null;
let tempRoot: string | null = null;

function emitComputeNode(payload: Record<string, unknown>) {
  eventHandlers.get('compute_node_event')?.({ payload });
}

function bootstrapInvokeDefaults(modelPath: string) {
  invokeMock.mockImplementation((command: string, args?: any) => {
    if (command === 'detect_backend') {
      return Promise.resolve({
        platform_label: 'linux',
        preferred_mode: 'cpu',
        display_label: 'cpu',
      });
    }
    if (command === 'load_config') {
      return Promise.resolve({
        model_path: modelPath,
        relay_base_url: relayUrl,
        preferred_mode: 'cpu',
      });
    }
    if (command === 'get_compute_node_status') {
      return Promise.resolve({
        running: false,
        registered: false,
        active_relay_url: '',
        backend_mode: 'cpu',
        model_path: modelPath,
        last_error: null,
      });
    }
    if (command === 'inspect_model_artifact') {
      return Promise.resolve({
        canonical_family_url: 'https://example.test/models',
        filename: path.basename(modelPath),
        url: 'https://example.test/model.gguf',
        models_dir: path.dirname(modelPath),
        resolved_model_path: modelPath,
        exists: true,
        size_bytes: 1,
      });
    }
    if (command === 'save_config') {
      return Promise.resolve(undefined);
    }
    if (command === 'start_compute_node') {
      const request = args?.request;
      return new Promise<void>((resolve, reject) => {
        const repoRoot = path.resolve(__dirname, '..', '..');
        const script = path.resolve(repoRoot, 'desktop-tauri/src-tauri/python/compute_node_bridge.py');
        const python = process.env.PYTHON_BIN ?? 'python3';
        bridgeChild = spawn(
          python,
          [
            script,
            '--model',
            request.model_path,
            '--mode',
            request.mode,
            '--relay-url',
            request.relay_base_url,
          ],
          {
            cwd: repoRoot,
            env: {
              ...process.env,
              USE_MOCK_LLM: '1',
            },
          }
        );

        let started = false;
        bridgeChild.stdout.on('data', (chunk: Buffer) => {
          for (const line of chunk.toString().split('\n').filter(Boolean)) {
            try {
              emitComputeNode(JSON.parse(line));
            } catch {
              // ignore malformed bridge output
            }
          }
          if (!started) {
            started = true;
            resolve();
          }
        });

        bridgeChild.once('error', (error) => reject(error));
        bridgeChild.stderr.on('data', (chunk: Buffer) => {
          emitComputeNode({ type: 'error', message: chunk.toString() });
        });
        bridgeChild.once('exit', (code) => {
          if (code !== 0) {
            emitComputeNode({ type: 'error', message: `bridge exited with ${code}` });
          }
        });
      });
    }
    if (command === 'stop_compute_node') {
      if (bridgeChild && !bridgeChild.killed) {
        bridgeChild.stdin.write('{"type":"cancel"}\n');
      }
      return Promise.resolve(undefined);
    }
    return Promise.resolve(undefined);
  });
}

describe('desktop operator e2e against local relay', () => {
  beforeEach(() => {
    cleanup();
    invokeMock.mockReset();
    listenMock.mockReset();
    eventHandlers.clear();

    listenMock.mockImplementation((event: string, handler: unknown) => {
      eventHandlers.set(event, handler as (evt: { payload: Record<string, unknown> }) => void);
      return Promise.resolve(() => {});
    });

    tempRoot = mkdtempSync(path.join(tmpdir(), 'token-place-desktop-e2e-'));
    const modelPath = path.join(tempRoot, 'model.gguf');
    writeFileSync(modelPath, 'x');
    bootstrapInvokeDefaults(modelPath);
  });

  afterEach(async () => {
    if (bridgeChild && !bridgeChild.killed) {
      bridgeChild.stdin.write('{"type":"cancel"}\n');
      await new Promise((resolve) => setTimeout(resolve, 200));
      if (!bridgeChild.killed) {
        bridgeChild.kill('SIGKILL');
      }
    }
    bridgeChild = null;
    if (tempRoot && existsSync(tempRoot)) {
      rmSync(tempRoot, { recursive: true, force: true });
    }
    tempRoot = null;
    cleanup();
  });

  maybeIt('starts operator from UI and reaches running state', async () => {
    render(<App />);

    const relayInput = (await screen.findByText('Relay URL')).parentElement?.querySelector('input');
    expect(relayInput).toBeTruthy();
    fireEvent.change(relayInput as HTMLInputElement, { target: { value: relayUrl } });

    const startOperatorButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    fireEvent.click(startOperatorButton);

    await waitFor(() => {
      expect(screen.getByText('Running:').textContent).toContain('yes');
    });
  }, 30000);
});
