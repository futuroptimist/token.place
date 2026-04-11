import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';

const invokeMock = vi.fn();
const listeners = new Map<string, Array<(event: { payload: unknown }) => void>>();

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (...args: unknown[]) => invokeMock(...args),
}));

vi.mock('@tauri-apps/api/event', () => ({
  listen: async (eventName: string, handler: (event: { payload: unknown }) => void) => {
    const existing = listeners.get(eventName) ?? [];
    existing.push(handler);
    listeners.set(eventName, existing);
    return () => {
      const current = listeners.get(eventName) ?? [];
      listeners.set(
        eventName,
        current.filter((candidate) => candidate !== handler)
      );
    };
  },
}));

vi.mock('@tauri-apps/plugin-dialog', () => ({
  open: vi.fn(),
}));

const baseInvoke = (cmd: string) => {
  if (cmd === 'detect_backend') {
    return Promise.resolve({
      platform_label: 'linux',
      preferred_mode: 'cpu',
      display_label: 'CPU',
    });
  }
  if (cmd === 'load_config') {
    return Promise.resolve({
      model_path: '/tmp/model.gguf',
      relay_base_url: 'https://token.place',
      preferred_mode: 'cpu',
    });
  }
  if (cmd === 'get_compute_node_status') {
    return Promise.resolve({
      running: false,
      registered: false,
      active_relay_url: '',
      backend_mode: 'cpu',
      model_path: '/tmp/model.gguf',
      last_error: null,
    });
  }
  if (cmd === 'inspect_model_artifact') {
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
};

describe('App startup failure UX', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    listeners.clear();
    invokeMock.mockReset();
    vi.stubGlobal('crypto', { randomUUID: () => 'request-1' });
    invokeMock.mockImplementation(baseInvoke);
  });

  it('marks local inference as failed when start invoke rejects', async () => {
    invokeMock.mockImplementation((cmd: string) => {
      if (cmd === 'start_inference') {
        return Promise.reject(new Error('python3 missing'));
      }
      return baseInvoke(cmd);
    });

    render(<App />);
    const promptInput = await waitFor(() => {
      const element = document.querySelector('textarea');
      if (!element) {
        throw new Error('prompt input not mounted');
      }
      return element;
    });
    fireEvent.change(promptInput as HTMLTextAreaElement, {
      target: { value: 'hello' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Start local inference' }));

    await waitFor(() => {
      expect(screen.getByText('failed')).toBeTruthy();
    });
    expect(screen.getByText(/python3 missing/)).toBeTruthy();
  });

  it('surfaces compute-node start failures in Last error', async () => {
    invokeMock.mockImplementation((cmd: string) => {
      if (cmd === 'start_compute_node') {
        return Promise.reject(new Error('relay protocol incompatible'));
      }
      return baseInvoke(cmd);
    });

    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: 'Start operator' }));

    await waitFor(() => {
      expect(screen.getAllByText(/relay protocol incompatible/).length).toBeGreaterThan(0);
    });
  });
});
