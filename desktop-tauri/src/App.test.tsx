import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';

const invokeMock = vi.fn();
const listenMock = vi.fn();
const eventHandlers = new Map<string, (evt: { payload: Record<string, unknown> }) => void>();
const testDir = path.dirname(fileURLToPath(import.meta.url));

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

vi.mock('@tauri-apps/api/event', () => ({
  listen: (...args: unknown[]) => listenMock(...args),
}));

vi.mock('@tauri-apps/plugin-dialog', () => ({
  open: vi.fn(),
}));

describe('desktop app start failure handling', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    invokeMock.mockReset();
    listenMock.mockReset();
    eventHandlers.clear();
    listenMock.mockImplementation((event: string, handler: unknown) => {
      eventHandlers.set(event, handler as (evt: { payload: Record<string, unknown> }) => void);
      return Promise.resolve(() => {});
    });

    invokeMock.mockImplementation((command: string) => {
      if (command === 'detect_backend') {
        return Promise.resolve({
          platform_label: 'macos',
          preferred_mode: 'auto',
          display_label: 'auto',
        });
      }
      if (command === 'load_config') {
        return Promise.resolve({
          model_path: '/tmp/model.gguf',
          relay_base_url: 'https://token.place',
          preferred_mode: 'auto',
        });
      }
      if (command === 'get_compute_node_status') {
        return Promise.resolve({
          running: false,
          registered: false,
          active_relay_url: '',
          backend_mode: 'auto',
          model_path: '',
          last_error: null,
        });
      }
      if (command === 'inspect_model_artifact') {
        return Promise.resolve({
          canonical_family_url: 'https://example.test/models',
          filename: 'model.gguf',
          url: 'https://example.test/model.gguf',
          models_dir: '/tmp',
          resolved_model_path: '/tmp/model.gguf',
          exists: true,
          size_bytes: 1,
        });
      }
      return Promise.resolve(undefined);
    });
  });

  it('moves local inference from starting to failed when invoke rejects', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_inference') {
        return Promise.reject(new Error('python runtime unavailable'));
      }
      return Promise.resolve(
        command === 'load_config'
          ? {
              model_path: '/tmp/model.gguf',
              relay_base_url: 'https://token.place',
              preferred_mode: 'auto',
            }
          : command === 'detect_backend'
            ? {
                platform_label: 'macos',
                preferred_mode: 'auto',
                display_label: 'auto',
              }
            : command === 'get_compute_node_status'
              ? {
                  running: false,
                  registered: false,
                  active_relay_url: '',
                  backend_mode: 'auto',
                  model_path: '',
                  last_error: null,
                }
              : {
                  canonical_family_url: 'https://example.test/models',
                  filename: 'model.gguf',
                  url: 'https://example.test/model.gguf',
                  models_dir: '/tmp',
                  resolved_model_path: '/tmp/model.gguf',
                  exists: true,
                  size_bytes: 1,
                }
      );
    });

    render(<App />);
    const promptArea = (await screen.findByText('Prompt'))
      .parentElement?.querySelector('textarea');
    expect(promptArea).toBeTruthy();
    fireEvent.change(promptArea as HTMLTextAreaElement, { target: { value: 'hello' } });
    const startInferenceButton = (await screen.findByText(
      'Start local inference'
    )) as HTMLButtonElement;
    await waitFor(() => expect(startInferenceButton.disabled).toBe(false));
    fireEvent.click(startInferenceButton);

    await waitFor(() =>
      expect(screen.getByText('Status:').textContent).toContain('failed')
    );
    expect(screen.getByText(/Error:/).textContent).toContain(
      'python runtime unavailable'
    );
  });

  it('surfaces compute-node start failures in last error', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_compute_node') {
        return Promise.reject(new Error('relay unreachable'));
      }
      if (command === 'detect_backend') {
        return Promise.resolve({
          platform_label: 'macos',
          preferred_mode: 'auto',
          display_label: 'auto',
        });
      }
      if (command === 'load_config') {
        return Promise.resolve({
          model_path: '/tmp/model.gguf',
          relay_base_url: 'https://token.place',
          preferred_mode: 'auto',
        });
      }
      if (command === 'get_compute_node_status') {
        return Promise.resolve({
          running: false,
          registered: false,
          active_relay_url: '',
          backend_mode: 'auto',
          model_path: '',
          last_error: null,
        });
      }
      return Promise.resolve({
        canonical_family_url: 'https://example.test/models',
        filename: 'model.gguf',
        url: 'https://example.test/model.gguf',
        models_dir: '/tmp',
        resolved_model_path: '/tmp/model.gguf',
        exists: true,
        size_bytes: 1,
      });
    });

    render(<App />);
    const startOperatorButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    fireEvent.click(startOperatorButton);
    await waitFor(() =>
      expect(screen.getByText(/Last error:/).textContent).toContain(
        'relay unreachable'
      )
    );
  });

  it(
    'marks local inference as failed on emitted error events after start invoke resolves',
    async () => {
    render(<App />);
    const promptArea = (await screen.findByText('Prompt'))
      .parentElement?.querySelector('textarea');
    expect(promptArea).toBeTruthy();
    fireEvent.change(promptArea as HTMLTextAreaElement, { target: { value: 'hello' } });
    const startInferenceButton = (await screen.findByText(
      'Start local inference'
    )) as HTMLButtonElement;
    await waitFor(() => expect(startInferenceButton.disabled).toBe(false));
    fireEvent.click(startInferenceButton);

    const inferenceHandler = eventHandlers.get('inference_event');
    expect(inferenceHandler).toBeTruthy();
    inferenceHandler?.({
      payload: {
        request_id: '00000000-0000-4000-8000-000000000000',
        type: 'error',
        message: 'ignore stale request',
      },
    });
    inferenceHandler?.({
      payload: {
        request_id: 'not-the-current-request',
        type: 'error',
        message: 'ignore stale request',
      },
    });

    const startInvocation = invokeMock.mock.calls.find((args) => args[0] === 'start_inference');
    expect(startInvocation).toBeTruthy();
    const currentRequestId = startInvocation?.[1]?.request?.request_id as string;
    inferenceHandler?.({
      payload: {
        request_id: currentRequestId,
        type: 'error',
        message: 'event failure path',
      },
    });

    await waitFor(() =>
      expect(screen.getByText('Status:').textContent).toContain('failed')
    );
    expect(screen.getByText(/Error:/).textContent).toContain('event failure path');
    },
  );

  it(
    'starts operator end-to-end against a local relay using packaged bridge layout',
    async () => {
      if (process.env.RUN_DESKTOP_OPERATOR_E2E !== '1') {
        return;
      }

      const pythonCommand = process.env.PYTHON ?? process.env.PYTHON3 ?? 'python3';
      const relayPort = 19567;
      const relayUrl = `http://127.0.0.1:${relayPort}`;
      const relayProcess = spawn(
        pythonCommand,
        [path.resolve(testDir, '../../relay.py'), '--port', String(relayPort), '--use_mock_llm'],
        { env: { ...process.env, TOKEN_PLACE_ENV: 'testing', USE_MOCK_LLM: '1' } },
      );

      const waitForRelay = async () => {
        const startedAt = Date.now();
        while (Date.now() - startedAt < 20_000) {
          try {
            const response = await fetch(`${relayUrl}/`);
            if (response.ok) {
              return;
            }
          } catch {
            // Relay is still booting.
          }
          await new Promise((resolve) => setTimeout(resolve, 250));
        }
        throw new Error('relay failed to start in test timeout');
      };

      let bridgeProcess: ChildProcessWithoutNullStreams | null = null;
      const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'token-place-packaged-'));
      const resourcesRoot = path.join(tempRoot, 'resources');
      const bridgePath = path.join(resourcesRoot, 'python', 'compute_node_bridge.py');

      await fs.mkdir(path.dirname(bridgePath), { recursive: true });
      await fs.cp(path.resolve(testDir, '../src-tauri/python/compute_node_bridge.py'), bridgePath);
      await fs.cp(path.resolve(testDir, '../../utils'), path.join(resourcesRoot, 'utils'), {
        recursive: true,
      });
      await fs.copyFile(
        path.resolve(testDir, '../../config.py'),
        path.join(resourcesRoot, 'config.py'),
      );

      const stopBridge = async () => {
        if (!bridgeProcess) {
          return;
        }
        bridgeProcess.stdin.write('{"type":"cancel"}\n');
        await new Promise((resolve) => setTimeout(resolve, 300));
        bridgeProcess.kill('SIGTERM');
      };

      try {
        await waitForRelay();
        const defaultInvoke = invokeMock.getMockImplementation();

        invokeMock.mockImplementation((command: string, args?: Record<string, unknown>) => {
          if (command === 'start_compute_node') {
            const request = (args?.request as Record<string, string>) ?? {};
            const handler = eventHandlers.get('compute_node_event');
            bridgeProcess = spawn(
              pythonCommand,
              [
                bridgePath,
                '--model',
                request.model_path,
                '--mode',
                request.mode,
                '--relay-url',
                request.relay_base_url,
              ],
              {
                cwd: resourcesRoot,
                env: {
                  ...process.env,
                  TOKEN_PLACE_ENV: 'testing',
                  USE_MOCK_LLM: '1',
                },
              },
            );
            bridgeProcess.stdout.on('data', (chunk) => {
              const lines = String(chunk)
                .split('\n')
                .map((line) => line.trim())
                .filter(Boolean);
              for (const line of lines) {
                try {
                  handler?.({ payload: JSON.parse(line) });
                } catch {
                  // Ignore malformed NDJSON test output.
                }
              }
            });
            return Promise.resolve(undefined);
          }
          if (command === 'stop_compute_node') {
            return stopBridge();
          }
          return defaultInvoke
            ? (defaultInvoke(command, args) as Promise<unknown>)
            : Promise.resolve(undefined);
        });

        render(<App />);
        const relayInput = (await screen.findByText('Relay URL'))
          .parentElement?.querySelector('input') as HTMLInputElement;
        fireEvent.change(relayInput, { target: { value: relayUrl } });

        const startOperatorButton =
          (await screen.findByText('Start operator')) as HTMLButtonElement;
        await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
        fireEvent.click(startOperatorButton);

        await waitFor(
          () => expect(screen.getByText(/Registered:/).textContent).toContain('yes'),
          { timeout: 25_000 },
        );
        expect(screen.getByText(/Last error:/).textContent).not.toContain('No module named');
      } finally {
        await stopBridge();
        relayProcess.kill('SIGTERM');
        await fs.rm(tempRoot, { recursive: true, force: true });
      }
    },
    40_000,
  );
});
