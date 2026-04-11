import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from './App';

const invokeMock = vi.fn();
const listeners = new Map<string, (event: { payload: Record<string, unknown> }) => void>();

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

vi.mock('@tauri-apps/api/event', () => ({
  listen: vi.fn((eventName: string, cb: (event: { payload: Record<string, unknown> }) => void) => {
    listeners.set(eventName, cb);
    return Promise.resolve(() => listeners.delete(eventName));
  }),
}));

vi.mock('@tauri-apps/plugin-dialog', () => ({
  open: vi.fn(),
}));

describe('App desktop failure handling', () => {
  beforeEach(() => {
    invokeMock.mockReset();
    listeners.clear();
    invokeMock.mockImplementation((command: string) => {
      if (command === 'detect_backend') {
        return Promise.resolve({
          platform_label: 'linux',
          preferred_mode: 'cpu',
          display_label: 'cpu',
        });
      }
      if (command === 'load_config') {
        return Promise.resolve({
          model_path: '/tmp/model.gguf',
          relay_base_url: 'https://token.place',
          preferred_mode: 'cpu',
        });
      }
      if (command === 'get_compute_node_status') {
        return Promise.resolve({
          running: false,
          registered: false,
          active_relay_url: '',
          backend_mode: 'cpu',
          model_path: '/tmp/model.gguf',
          last_error: null,
        });
      }
      if (command === 'inspect_model_artifact') {
        return Promise.resolve({
          canonical_family_url: 'https://example.com',
          filename: 'model.gguf',
          url: 'https://example.com/model.gguf',
          models_dir: '/tmp',
          resolved_model_path: '/tmp/model.gguf',
          exists: true,
          size_bytes: 123,
        });
      }
      return Promise.resolve(null);
    });
  });

  afterEach(() => {
    cleanup();
  });

  it('transitions to failed when start inference invoke rejects', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_inference') {
        return Promise.reject(new Error('python missing'));
      }
      return Promise.resolve(
        command === 'detect_backend'
          ? { platform_label: 'linux', preferred_mode: 'cpu', display_label: 'cpu' }
          : command === 'load_config'
            ? {
                model_path: '/tmp/model.gguf',
                relay_base_url: 'https://token.place',
                preferred_mode: 'cpu',
              }
            : command === 'get_compute_node_status'
              ? {
                  running: false,
                  registered: false,
                  active_relay_url: '',
                  backend_mode: 'cpu',
                  model_path: '/tmp/model.gguf',
                  last_error: null,
                }
              : {
                  canonical_family_url: 'https://example.com',
                  filename: 'model.gguf',
                  url: 'https://example.com/model.gguf',
                  models_dir: '/tmp',
                  resolved_model_path: '/tmp/model.gguf',
                  exists: true,
                  size_bytes: 123,
                }
      );
    });

    const { container } = render(<App />);
    const prompt = container.querySelector('textarea');
    expect(prompt).not.toBeNull();
    fireEvent.change(prompt!, { target: { value: 'hello' } });

    const startButton = screen.getByRole('button', { name: 'Start local inference' });
    await waitFor(() => expect(startButton.hasAttribute('disabled')).toBe(false));
    fireEvent.click(startButton);

    await waitFor(() => {
      const statusLine = screen.getByText(/^Status:/).parentElement;
      expect(statusLine?.textContent).toContain('failed');
    });
    expect(screen.getByText(/Error:/).textContent).toContain('python missing');
  });

  it('shows compute-node startup errors from compute_node_event', async () => {
    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: 'Start operator' }));

    const cb = listeners.get('compute_node_event');
    expect(cb).toBeDefined();
    cb?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        last_error: 'relay response incompatible with desktop-v0.1.0 operator bridge',
      },
    });

    await waitFor(() => {
      expect(screen.getByText(/Last error:/).textContent).toContain(
        'relay response incompatible with desktop-v0.1.0 operator bridge',
      );
    });
  });
});
