import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
          available_backend: 'metal',
          availability_label: 'Metal-capable platform (Apple Silicon)',
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
          requested_mode: 'auto',
          effective_mode: 'cpu',
          backend_available: 'unknown',
          backend_selected: 'cpu',
          backend_used: 'cpu',
          fallback_reason: null,
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
                available_backend: 'metal',
                availability_label: 'Metal-capable platform (Apple Silicon)',
              }
            : command === 'get_compute_node_status'
              ? {
                  running: false,
                  registered: false,
                  active_relay_url: '',
                  requested_mode: 'auto',
                  effective_mode: 'cpu',
                  backend_available: 'unknown',
                  backend_selected: 'cpu',
                  backend_used: 'cpu',
                  fallback_reason: null,
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
          available_backend: 'metal',
          availability_label: 'Metal-capable platform (Apple Silicon)',
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
          requested_mode: 'auto',
          effective_mode: 'cpu',
          backend_available: 'unknown',
          backend_selected: 'cpu',
          backend_used: 'cpu',
          fallback_reason: null,
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

  it('marks local inference as failed on emitted error events after start invoke resolves', async () => {
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
  });
});
