import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';

const invokeMock = vi.fn();
const listenMock = vi.fn();

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
    listenMock.mockResolvedValue(() => {});

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
    fireEvent.click(screen.getByText('Start local inference'));

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
    fireEvent.click(await screen.findByText('Start operator'));
    await waitFor(() =>
      expect(screen.getByText(/Last error:/).textContent).toContain(
        'relay unreachable'
      )
    );
  });
});
