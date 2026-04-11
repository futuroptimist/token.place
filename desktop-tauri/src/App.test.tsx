import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

describe('App desktop start failure handling', () => {
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
          canonical_family_url: 'https://example.invalid/model',
          filename: 'model.gguf',
          url: 'https://example.invalid/model.gguf',
          models_dir: '/tmp',
          resolved_model_path: '/tmp/model.gguf',
          exists: true,
          size_bytes: 1,
        });
      }
      return Promise.resolve(undefined);
    });
  });

  it('transitions local inference start to failed when invoke rejects', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_inference') {
        return Promise.reject(new Error('sidecar spawn failed'));
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
                  canonical_family_url: 'https://example.invalid/model',
                  filename: 'model.gguf',
                  url: 'https://example.invalid/model.gguf',
                  models_dir: '/tmp',
                  resolved_model_path: '/tmp/model.gguf',
                  exists: true,
                  size_bytes: 1,
                }
      );
    });

    render(<App />);

    await screen.findByText(/Detected backend:/);
    const textboxes = screen.getAllByRole('textbox');
    fireEvent.change(textboxes[textboxes.length - 1], { target: { value: 'hello' } });
    fireEvent.click(screen.getByRole('button', { name: 'Start local inference' }));

    expect(screen.getByText('starting')).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByText('failed')).toBeTruthy();
      expect(screen.getByText(/sidecar spawn failed/)).toBeTruthy();
    });
  });

  it('surfaces compute-node start invoke failures in status and last error', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_compute_node') {
        return Promise.reject(new Error('relay /sink 404'));
      }
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
      return Promise.resolve({
        canonical_family_url: 'https://example.invalid/model',
        filename: 'model.gguf',
        url: 'https://example.invalid/model.gguf',
        models_dir: '/tmp',
        resolved_model_path: '/tmp/model.gguf',
        exists: true,
        size_bytes: 1,
      });
    });

    render(<App />);

    await screen.findByRole('button', { name: 'Start operator' });
    fireEvent.click(screen.getByRole('button', { name: 'Start operator' }));

    await waitFor(() => {
      const operatorHeading = screen.getByRole('heading', { name: 'Compute node operator' });
      const operatorSection = operatorHeading.closest('section');
      expect(operatorSection).toBeTruthy();
      expect(within(operatorSection as HTMLElement).getByText(/Last error:/)).toBeTruthy();
      expect(
        within(operatorSection as HTMLElement).getByText('Error: relay /sink 404')
      ).toBeTruthy();
    });
  });
});
