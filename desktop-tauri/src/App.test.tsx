import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App, normalizeDesktopConfig } from './App';

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

  it('normalizes untrusted relay URLs and preferred mode from persisted config', () => {
    expect(
      normalizeDesktopConfig({
        model_path: 123,
        relay_base_url: 42,
        relay_base_urls: [' https://token.place ', 123, 'https://staging.token.place'],
        preferred_mode: 'bogus',
      })
    ).toEqual({
      model_path: '',
      relay_base_url: 'https://token.place',
      relay_base_urls: ['https://token.place', 'https://staging.token.place'],
      preferred_mode: 'auto',
      context_tier: '8k-fast',
    });
  });

  const mockInitialCommand = (command: string) => {
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
        log_file_path: null,
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
  };

  const mockInitialComputeStatus = (
    statusOverrides: Record<string, unknown>,
    configOverrides: Record<string, unknown> = {}
  ) => {
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
          ...configOverrides,
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
          ...statusOverrides,
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
  };

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

  it('blocks duplicate start clicks while compute-node startup is pending', async () => {
    let resolveStart: (() => void) | undefined;
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_compute_node') {
        return new Promise<void>((resolve) => {
          resolveStart = resolve;
        });
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
    const stopOperatorButton = (await screen.findByText('Stop operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));

    fireEvent.click(startOperatorButton);
    fireEvent.click(startOperatorButton);

    expect(
      invokeMock.mock.calls.filter(([command]) => command === 'start_compute_node')
    ).toHaveLength(1);
    expect(startOperatorButton.disabled).toBe(true);
    expect(stopOperatorButton.disabled).toBe(true);

    resolveStart?.();
  });

  it('keeps model path blank on first launch when config has no persisted model path', async () => {
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
          model_path: '',
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
          resolved_model_path: 'C:\\Users\\dev\\Downloads\\model.gguf',
          exists: false,
          size_bytes: null,
        });
      }
      return Promise.resolve(undefined);
    });

    render(<App />);
    const modelInput = (await screen.findByLabelText('Model GGUF path')) as HTMLInputElement;
    await waitFor(() => expect((modelInput as HTMLInputElement).value).toBe(''));
    expect(
      invokeMock.mock.calls.some(
        (call) =>
          call[0] === 'save_config' &&
          typeof call[1]?.config?.model_path === 'string' &&
          call[1].config.model_path.includes('C:\\Users\\dev')
      )
    ).toBe(false);
  });

  it('restores persisted user-selected model path from saved config', async () => {
    render(<App />);
    const modelInput = (await screen.findByLabelText('Model GGUF path')) as HTMLInputElement;
    await waitFor(() => expect((modelInput as HTMLInputElement).value).toBe('/tmp/model.gguf'));
  });

  it('surfaces bridge startup exits through compute_node_event errors', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_compute_node') {
        return Promise.resolve(undefined);
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

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        last_error:
          'compute-node bridge exited with status 0 before emitting a startup event',
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));
    expect(screen.getByText(/Last error:/).textContent).toContain(
      'before emitting a startup event'
    );
  });

  it('keeps running state healthy when started and status events are healthy', async () => {
    render(<App />);
    const startOperatorButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    fireEvent.click(startOperatorButton);

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        last_error: null,
      },
    });
    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: true,
        relay_runtime_state: 'ready',
        warm_load_state: 'ready',
        last_error: null,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    await waitFor(() => expect(screen.getByText(/Registered:/).textContent).toContain('yes'));
    expect(screen.getByText(/Last error:/).textContent).toContain('none');
  });


  it('does not display registered yes while relay runtime is warming', async () => {
    render(<App />);
    await screen.findByText('Start operator');

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: true,
        warm_load_state: 'warming',
        effective_mode: 'pending',
        backend_available: 'unknown',
        backend_selected: 'unknown',
        backend_used: 'unknown',
        last_error: null,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('warming');
  });


  it('replays cached warming readiness and relay runtime path without showing registered yes', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: true,
      active_relay_url: 'https://token.place',
      model_path: '/tmp/model.gguf',
      warm_load_state: 'warming',
      warm_load_enabled: true,
      warm_load_duration_ms: 25,
      runtime_path: 'sidecar',
      relay_runtime_path: 'bridge',
    });

    render(<App />);

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('warming');
    expect(screen.getByText(/Relay runtime path:/).textContent).toContain('bridge');
  });

  it('allows cached ready relay runtime status to display registered yes', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: true,
      active_relay_url: 'https://token.place',
      model_path: '/tmp/model.gguf',
      warm_load_state: 'ready',
      warm_load_enabled: true,
      warm_load_duration_ms: 25,
      runtime_path: 'bridge',
      relay_runtime_path: 'bridge',
    });

    render(<App />);

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Registered:/).textContent).toContain('yes');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('ready');
  });

  it('allows cached registered status when warm-load is disabled', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: true,
      active_relay_url: 'https://token.place',
      model_path: '/tmp/model.gguf',
      warm_load_state: 'not_started',
      warm_load_enabled: false,
      warm_load_duration_ms: null,
      runtime_path: 'bridge',
      relay_runtime_path: 'bridge',
    });

    render(<App />);

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Registered:/).textContent).toContain('yes');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('not_started');
  });

  it('does not display registered yes for stopped cached compute node status', async () => {
    mockInitialComputeStatus({
      running: false,
      registered: true,
      active_relay_url: 'https://token.place',
      model_path: '/tmp/model.gguf',
      warm_load_state: 'ready',
      warm_load_enabled: true,
      warm_load_duration_ms: 25,
      runtime_path: 'bridge',
      relay_runtime_path: 'bridge',
    });

    render(<App />);

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
  });



  it('renders operator debug log affordances from live events and opens in-app log text', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'read_operator_log') {
        return Promise.resolve('desktop.compute_node.stderr bridge stderr line');
      }
      if (command === 'reveal_operator_log' || command === 'open_operator_debug_terminal') {
        return Promise.resolve(undefined);
      }
      return mockInitialCommand(command);
    });

    render(<App />);

    const openLogButton = (await screen.findByText('Open debug log')) as HTMLButtonElement;
    expect(openLogButton.disabled).toBe(true);

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        active_relay_url: 'https://token.place',
        relay_runtime_state: 'starting',
        log_file_path: '/Users/Example User/Library/Logs/token.place/operator/compute-node-1.log',
        operator_session_id: 'session-1',
        sequence: 1,
      },
    });

    await waitFor(() => expect(openLogButton.disabled).toBe(false));
    expect(screen.getByText('Reveal log file')).toBeTruthy();
    expect(screen.getByText('Copy log path')).toBeTruthy();
    expect(screen.getByText('Open debug terminal')).toBeTruthy();

    fireEvent.click(openLogButton);

    await screen.findByLabelText('Operator debug console');
    expect(screen.getByDisplayValue(/bridge stderr line/)).toBeTruthy();
  });


  it('clears stale operator log path when backend emits null', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: false,
      active_relay_url: 'https://token.place',
      log_file_path: '/Users/Example User/Library/Logs/token.place/operator/compute-node-old.log',
      operator_session_id: 'session-1',
      sequence: 1,
    });

    render(<App />);
    await waitFor(() =>
      expect(screen.getByText(/Operator debug log:/).textContent).toContain('compute-node-old.log')
    );

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        log_file_path: null,
        message: 'operator log unavailable',
        operator_session_id: 'session-1',
        sequence: 2,
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Operator debug log:/).textContent).toContain('not created yet')
    );
    expect((screen.getByText('Open debug log') as HTMLButtonElement).disabled).toBe(true);
  });


  it('copies operator log path and surfaces path copy failures', async () => {
    const originalClipboard = navigator.clipboard;
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    mockInitialComputeStatus({
      running: true,
      registered: false,
      active_relay_url: 'https://token.place',
      log_file_path: '/Users/Example User/Library/Logs/token.place/operator/compute-node-1.log',
    });

    render(<App />);
    fireEvent.click(await screen.findByText('Copy log path'));

    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        '/Users/Example User/Library/Logs/token.place/operator/compute-node-1.log'
      )
    );

    writeText.mockRejectedValueOnce(new Error('path clipboard denied'));
    fireEvent.click(screen.getByText('Copy log path'));

    await waitFor(() => expect(screen.getByText(/path clipboard denied/)).toBeTruthy());
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: originalClipboard });
  });

  it('surfaces clipboard copy failures in the operator debug console', async () => {
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: vi.fn(() => Promise.reject(new Error('clipboard denied'))) },
    });
    invokeMock.mockImplementation((command: string) => {
      if (command === 'get_compute_node_status') {
        return Promise.resolve({
          running: true,
          registered: false,
          active_relay_url: 'https://token.place',
          requested_mode: 'auto',
          effective_mode: 'cpu',
          backend_available: 'unknown',
          backend_selected: 'cpu',
          backend_used: 'cpu',
          fallback_reason: null,
          model_path: '/tmp/model.gguf',
          last_error: null,
          relay_runtime_state: 'starting',
          log_file_path: '/Users/Example User/Library/Logs/token.place/operator/compute-node-1.log',
        });
      }
      if (command === 'read_operator_log') {
        return Promise.resolve('desktop.compute_node.stderr bridge stderr line');
      }
      return mockInitialCommand(command);
    });

    render(<App />);
    fireEvent.click(await screen.findByText('Open debug log'));
    await screen.findByLabelText('Operator debug console');
    fireEvent.click(screen.getByText('Copy log'));

    await waitFor(() => expect(screen.getByText(/clipboard denied/)).toBeTruthy());
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: originalClipboard });
  });

  it('invokes backend stop and reflects stopped operator event', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: true,
      active_relay_url: 'https://token.place',
      warm_load_state: 'ready',
      warm_load_enabled: true,
    });

    render(<App />);
    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Registered:/).textContent).toContain('yes');

    const stopOperatorButton = (await screen.findByText('Stop operator')) as HTMLButtonElement;
    fireEvent.click(stopOperatorButton);

    await waitFor(() =>
      expect(invokeMock.mock.calls.some(([command]) => command === 'stop_compute_node')).toBe(true)
    );

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'stopped',
        running: false,
        registered: false,
        active_relay_url: 'https://token.place',
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
  });


  it('handles Start Stop Start with fresh registration and cleared errors', async () => {
    mockInitialComputeStatus({
      running: false,
      registered: false,
      active_relay_url: 'https://token.place',
      relay_runtime_state: 'idle',
      last_error: 'previous failure',
      operator_session_id: 'old-session',
      sequence: 8,
    });

    render(<App />);
    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();

    const startButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    fireEvent.click(startButton);
    await waitFor(() =>
      expect(invokeMock.mock.calls.some(([command]) => command === 'start_compute_node')).toBe(true)
    );
    expect(screen.getByText(/Running:/).textContent).toContain('no');
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.queryByText(/Previous failure/i)).toBeNull();

    computeHandler?.({
      payload: {
        type: 'status',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        last_error: 'old stale stop',
        operator_session_id: 'old-session',
        sequence: 9,
      },
    });
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('starting');

    computeHandler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        relay_runtime_state: 'warming',
        active_relay_url: 'https://token.place',
        last_error: null,
        operator_session_id: 'session-1',
        sequence: 1,
      },
    });
    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');

    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: true,
        relay_runtime_state: 'ready',
        active_relay_url: 'https://token.place',
        last_error: null,
        operator_session_id: 'session-1',
        sequence: 2,
      },
    });
    await waitFor(() => expect(screen.getByText(/Registered:/).textContent).toContain('yes'));

    fireEvent.click((await screen.findByText('Stop operator')) as HTMLButtonElement);
    await waitFor(() =>
      expect(invokeMock.mock.calls.some(([command]) => command === 'stop_compute_node')).toBe(true)
    );
    computeHandler?.({
      payload: {
        type: 'stopped',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        last_error: null,
        operator_session_id: 'session-1',
        sequence: 3,
      },
    });
    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');

    fireEvent.click((await screen.findByText('Start operator')) as HTMLButtonElement);
    computeHandler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        relay_runtime_state: 'warming',
        active_relay_url: 'https://token.place',
        last_error: null,
        operator_session_id: 'session-2',
        sequence: 1,
      },
    });
    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');

    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: true,
        relay_runtime_state: 'ready',
        active_relay_url: 'https://token.place',
        last_error: null,
        operator_session_id: 'session-2',
        sequence: 2,
      },
    });
    await waitFor(() => expect(screen.getByText(/Registered:/).textContent).toContain('yes'));
  });

  it('surfaces a fresh restart startup error while ignoring stale old-session events', async () => {
    mockInitialComputeStatus({
      running: false,
      registered: false,
      active_relay_url: 'https://token.place',
      relay_runtime_state: 'idle',
      last_error: null,
      operator_session_id: 'old-session',
      sequence: 8,
    });

    render(<App />);
    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();

    const startButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    fireEvent.click(startButton);
    computeHandler?.({
      payload: {
        type: 'started',
        running: true,
        registered: true,
        relay_runtime_state: 'ready',
        active_relay_url: 'https://token.place',
        last_error: null,
        operator_session_id: 'session-1',
        sequence: 1,
      },
    });
    await waitFor(() => expect(screen.getByText(/Registered:/).textContent).toContain('yes'));

    fireEvent.click((await screen.findByText('Stop operator')) as HTMLButtonElement);
    computeHandler?.({
      payload: {
        type: 'stopped',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        last_error: null,
        operator_session_id: 'session-1',
        sequence: 2,
      },
    });
    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));

    fireEvent.click((await screen.findByText('Start operator')) as HTMLButtonElement);
    expect(((await screen.findByText('Start operator')) as HTMLButtonElement).disabled).toBe(true);

    computeHandler?.({
      payload: {
        type: 'status',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        last_error: 'old stale stop after restart',
        operator_session_id: 'session-1',
        sequence: 3,
      },
    });
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        relay_runtime_state: 'failed',
        last_error: 'old stale error after restart',
        operator_session_id: 'session-1',
        sequence: 4,
      },
    });
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('starting');
    expect(screen.queryByText('old stale error after restart')).toBeNull();
    expect(((await screen.findByText('Start operator')) as HTMLButtonElement).disabled).toBe(true);

    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        last_error: 'new session failed before started',
        operator_session_id: 'session-2',
        sequence: 1,
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Last error:/).textContent).toContain(
        'new session failed before started'
      )
    );
    expect(screen.getByText(/Running:/).textContent).toContain('no');
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('failed');
    expect(((await screen.findByText('Start operator')) as HTMLButtonElement).disabled).toBe(false);
  });

  it('renders the full initial idle compute-node status contract', async () => {
    mockInitialComputeStatus({
      running: false,
      registered: false,
      active_relay_url: 'https://token.place',
      relay_runtime_state: 'idle',
      runtime_path: null,
      relay_runtime_path: null,
      requested_mode: 'auto',
      effective_mode: null,
      backend_available: null,
      backend_selected: null,
      backend_used: null,
      fallback_reason: null,
      model_path: '',
      last_error: null,
    });

    render(<App />);

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('idle');
    expect(screen.getByText(/Runtime path:/).textContent).toContain('pending');
    expect(screen.getByText(/Relay runtime path:/).textContent).toContain('pending');
    expect(screen.getByText(/Effective mode:/).textContent).toContain('pending');
    expect(screen.getByText(/Backend available:/).textContent).toContain('pending');
    expect(screen.getByText(/Backend selected:/).textContent).toContain('pending');
    expect(screen.getByText(/Backend used:/).textContent).toContain('pending');
    expect(screen.getByText(/Fallback reason:/).textContent).toContain('none');
    expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain('none');
    expect(screen.getByText(/Last error:/).textContent).toContain('none');
  });

  it('merges compute-node readiness diagnostics into the desktop status snapshot', async () => {
    render(<App />);
    await screen.findByText('Start operator');

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        last_error: 'warm load failed',
        readiness_diagnostics: {
          api_v1_readiness_completion_smoke_method: 'create_completion_keyword_prompt',
          api_v1_readiness_completion_smoke_rejected_option: 'temperature',
          api_v1_readiness_completion_smoke_internal_reason: 'SECRET_PROMPT',
          unsafe_nested: { prompt: 'secret' },
        },
        operator_session_id: 'session-1',
        sequence: 1,
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain(
        'api_v1_readiness_completion_smoke_method=create_completion_keyword_prompt'
      )
    );
    expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain(
      'api_v1_readiness_completion_smoke_rejected_option=temperature'
    );
    expect(screen.getByText(/Readiness diagnostics:/).textContent).not.toContain('unsafe_nested');
    expect(screen.getByText(/Readiness diagnostics:/).textContent).not.toContain(
      'api_v1_readiness_completion_smoke_internal_reason'
    );
    expect(screen.getByText(/Readiness diagnostics:/).textContent).not.toContain('SECRET_PROMPT');
  });

  it('merges flat compute-node readiness diagnostics into the desktop status snapshot', async () => {
    render(<App />);
    await screen.findByText('Start operator');

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        last_error: 'warm load failed',
        api_v1_readiness_completion_smoke_method: 'create_completion_keyword_prompt',
        api_v1_readiness_completion_smoke_attempted_generation_kwargs: 'max_tokens',
        api_v1_readiness_completion_smoke_internal_reason: 'SECRET_PROMPT',
        api_v1_readiness_error_reason: 'contains spaces and should be dropped',
        unsafe_nested: { prompt: 'secret' },
        operator_session_id: 'session-1',
        sequence: 1,
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain(
        'api_v1_readiness_completion_smoke_method=create_completion_keyword_prompt'
      )
    );
    expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain(
      'api_v1_readiness_completion_smoke_attempted_generation_kwargs=max_tokens'
    );
    expect(screen.getByText(/Readiness diagnostics:/).textContent).not.toContain('unsafe_nested');
    expect(screen.getByText(/Readiness diagnostics:/).textContent).not.toContain(
      'api_v1_readiness_completion_smoke_internal_reason'
    );
    expect(screen.getByText(/Readiness diagnostics:/).textContent).not.toContain('SECRET_PROMPT');
    expect(screen.getByText(/Readiness diagnostics:/).textContent).not.toContain('contains spaces');
  });

  it('clears readiness diagnostics on diagnostic-free status and error events', async () => {
    render(<App />);
    await screen.findByText('Start operator');

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        last_error: 'warm load failed',
        api_v1_readiness_completion_smoke_method: 'create_completion_keyword_prompt',
        operator_session_id: 'session-1',
        sequence: 1,
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain(
        'api_v1_readiness_completion_smoke_method=create_completion_keyword_prompt'
      )
    );

    computeHandler?.({
      payload: {
        type: 'status',
        running: false,
        relay_runtime_state: 'idle',
        last_error: null,
        operator_session_id: 'session-1',
        sequence: 2,
      },
    });

    await waitFor(() => expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain('none'));

    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        last_error: 'later failure without diagnostics',
        operator_session_id: 'session-1',
        sequence: 3,
      },
    });

    await waitFor(() => expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain('none'));
  });

  it('renders ready-but-not-registered and registered runtime diagnostics from bridge events', async () => {
    render(<App />);
    await screen.findByText('Start operator');

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: false,
        relay_runtime_state: 'ready',
        active_relay_url: 'https://relay.example',
        requested_mode: 'gpu',
        effective_mode: 'gpu',
        backend_available: 'cuda',
        backend_selected: 'cuda',
        backend_used: 'cuda',
        fallback_reason: null,
        model_path: '/models/cuda.gguf',
        runtime_path: 'sidecar',
        relay_runtime_path: 'bridge',
        last_error: null,
        operator_session_id: 'session-1',
        sequence: 1,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('ready');
    expect(screen.getByText(/Runtime path:/).textContent).toContain('sidecar');
    expect(screen.getByText(/Relay runtime path:/).textContent).toContain('bridge');
    expect(screen.getByText(/Active relay URL:/).textContent).toContain('https://relay.example');
    expect(screen.getByText(/Requested mode:/).textContent).toContain('gpu');
    expect(screen.getByText(/Backend used:/).textContent).toContain('cuda');

    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: true,
        relay_runtime_state: 'ready',
        active_relay_url: 'https://relay.example',
        requested_mode: 'gpu',
        effective_mode: 'gpu',
        backend_available: 'cuda',
        backend_selected: 'cuda',
        backend_used: 'cuda',
        fallback_reason: null,
        model_path: '/models/cuda.gguf',
        runtime_path: 'sidecar',
        relay_runtime_path: 'bridge',
        last_error: null,
        operator_session_id: 'session-1',
        sequence: 2,
      },
    });

    await waitFor(() => expect(screen.getByText(/Registered:/).textContent).toContain('yes'));
    expect(screen.getByText(/Last error:/).textContent).toContain('none');
  });

  it('renders processing, stopped, and failure lifecycle events accurately', async () => {
    render(<App />);
    await screen.findByText('Start operator');

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: true,
        relay_runtime_state: 'processing',
        active_relay_url: 'https://token.place',
        requested_mode: 'auto',
        effective_mode: 'gpu',
        backend_available: 'cuda',
        backend_selected: 'cuda',
        backend_used: 'cuda',
        fallback_reason: null,
        model_path: '/tmp/model.gguf',
        runtime_path: 'bridge',
        relay_runtime_path: 'bridge',
        last_error: null,
        operator_session_id: 'session-2',
        sequence: 1,
      },
    });

    await waitFor(() => expect(screen.getByText(/Relay runtime state:/).textContent).toContain('processing'));
    expect(screen.getByText(/Registered:/).textContent).toContain('yes');

    computeHandler?.({
      payload: {
        type: 'stopped',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        active_relay_url: 'https://token.place',
        last_error: null,
        operator_session_id: 'session-2',
        sequence: 2,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('stopped');
    expect(screen.getByText(/Last error:/).textContent).toContain('none');

    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        relay_runtime_state: 'failed',
        last_error: 'runtime failed to initialize',
        operator_session_id: 'session-2',
        sequence: 3,
      },
    });

    await waitFor(() => expect(screen.getByText(/Relay runtime state:/).textContent).toContain('failed'));
    expect(screen.getByText(/Last error:/).textContent).toContain('runtime failed to initialize');
  });

  it('ignores stale compute-node events from a prior operator session', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: false,
      relay_runtime_state: 'warming',
      active_relay_url: 'https://token.place',
      model_path: '/tmp/model.gguf',
      warm_load_enabled: true,
      operator_session_id: 'new-session',
      sequence: 4,
    });

    render(<App />);
    await waitFor(() => expect(screen.getByText(/Relay runtime state:/).textContent).toContain('warming'));

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'status',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        last_error: 'old process failed after restart',
        operator_session_id: 'old-session',
        sequence: 99,
      },
    });
    computeHandler?.({
      payload: {
        type: 'status',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        last_error: 'older sequence failed after restart',
        operator_session_id: 'new-session',
        sequence: 3,
      },
    });
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        relay_runtime_state: 'failed',
        last_error: 'old error side effect after restart',
        operator_session_id: 'old-session',
        sequence: 100,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('warming');
    expect(screen.getByText(/Last error:/).textContent).toContain('none');
    expect(screen.queryByText('old error side effect after restart')).toBeNull();
  });

  it('ignores duplicate sequence events for the current operator session', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: true,
      relay_runtime_state: 'ready',
      active_relay_url: 'https://token.place',
      model_path: '/tmp/model.gguf',
      operator_session_id: 'session-1',
      sequence: 5,
    });

    render(<App />);
    await waitFor(() => expect(screen.getByText(/Relay runtime state:/).textContent).toContain('ready'));

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'status',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        last_error: 'duplicate sequence replay',
        operator_session_id: 'session-1',
        sequence: 5,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('ready');
    expect(screen.getByText(/Last error:/).textContent).toContain('none');
  });

  it('accepts a fresh started event for restart after stop', async () => {
    mockInitialComputeStatus({
      running: false,
      registered: false,
      relay_runtime_state: 'stopped',
      active_relay_url: 'https://token.place',
      operator_session_id: 'old-session',
      sequence: 8,
    });

    render(<App />);
    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        relay_runtime_state: 'starting',
        active_relay_url: 'https://token.place',
        model_path: '/tmp/model.gguf',
        operator_session_id: 'new-session',
        sequence: 1,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('starting');
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
  });

  it('surfaces fresh restart errors from a new operator session', async () => {
    mockInitialComputeStatus({
      running: false,
      registered: false,
      relay_runtime_state: 'stopped',
      active_relay_url: 'https://token.place',
      last_error: null,
      operator_session_id: 'old-session',
      sequence: 8,
    });

    render(<App />);
    const startButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startButton.disabled).toBe(false));
    fireEvent.click(startButton);

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        relay_runtime_state: 'failed',
        last_error: 'fresh preflight failed',
        operator_session_id: 'new-session',
        sequence: 1,
      },
    });

    await waitFor(() =>
      expect(screen.getByText(/Last error:/).textContent).toContain('fresh preflight failed')
    );
    await waitFor(() => expect(startButton.disabled).toBe(false));
    expect(screen.getByText(/Running:/).textContent).toContain('no');
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('failed');
  });

  it('ignores replayed started events from the stopped prior session', async () => {
    mockInitialComputeStatus({
      running: false,
      registered: false,
      relay_runtime_state: 'stopped',
      active_relay_url: 'https://token.place',
      operator_session_id: 'old-session',
      sequence: 8,
    });

    render(<App />);
    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        relay_runtime_state: 'starting',
        active_relay_url: 'https://token.place',
        model_path: '/tmp/old-model.gguf',
        operator_session_id: 'old-session',
        sequence: 1,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('no'));
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('stopped');
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

  it('renders one relay URL field from legacy config and migrates it on save', async () => {
    render(<App />);

    const relayInput = (await screen.findByLabelText('Relay URL 1')) as HTMLInputElement;
    await waitFor(() => expect(relayInput.value).toBe('https://token.place'));
    expect(screen.getByText(/Configured relay URLs:/).textContent).toContain('https://token.place');

    await waitFor(() =>
      expect(
        invokeMock.mock.calls.some(
          ([command, args]) =>
            command === 'save_config' &&
            args?.config?.relay_base_url === 'https://token.place' &&
            Array.isArray(args?.config?.relay_base_urls) &&
            args.config.relay_base_urls.length === 1 &&
            args.config.relay_base_urls[0] === 'https://token.place'
        )
      ).toBe(true)
    );
  });

  it('loads and displays multiple persisted relay URLs', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'load_config') {
        return Promise.resolve({
          model_path: '/tmp/model.gguf',
          relay_base_url: 'https://token.place',
          relay_base_urls: ['https://token.place', 'https://staging.token.place'],
          preferred_mode: 'auto',
        });
      }
      return mockInitialCommand(command);
    });

    render(<App />);

    expect((await screen.findByLabelText('Relay URL 1') as HTMLInputElement).value).toBe('https://token.place');
    expect((await screen.findByLabelText('Relay URL 2') as HTMLInputElement).value).toBe('https://staging.token.place');
    expect(screen.getByText(/Configured relay URLs:/).textContent).toContain('https://token.place, https://staging.token.place');
  });

  it('adds and removes relay URL fields while keeping one field', async () => {
    render(<App />);

    const addButton = await screen.findByText('Add new relay URL');
    fireEvent.click(addButton);

    const secondRelayInput = (await screen.findByLabelText('Relay URL 2')) as HTMLInputElement;
    expect(secondRelayInput.value).toBe('');
    fireEvent.change(secondRelayInput, { target: { value: ' https://staging.token.place ' } });

    const deleteSecondRelayButton = await screen.findByLabelText('Delete relay URL 2');
    fireEvent.click(deleteSecondRelayButton);

    await waitFor(() => expect(screen.queryByLabelText('Relay URL 2')).toBeNull());
    expect(screen.getByLabelText('Relay URL 1')).toBeTruthy();
    expect(screen.queryByText('Delete')).toBeNull();
  });

  it('enforces the documented maximum of 10 relay URL fields', async () => {
    render(<App />);

    const addButton = (await screen.findByText('Add new relay URL')) as HTMLButtonElement;
    expect(await screen.findByLabelText('Relay URL 1')).toBeTruthy();

    for (let relayNumber = 2; relayNumber <= 10; relayNumber += 1) {
      expect(addButton.disabled).toBe(false);
      fireEvent.click(addButton);
      expect(await screen.findByLabelText(`Relay URL ${relayNumber}`)).toBeTruthy();
    }

    expect(screen.queryByLabelText('Relay URL 11')).toBeNull();
    expect(addButton.disabled).toBe(true);
  });

  it('disables relay URL editing while the operator is running', async () => {
    mockInitialComputeStatus({ running: true, registered: true, relay_runtime_state: 'ready' });

    render(<App />);

    const relayInput = (await screen.findByLabelText('Relay URL 1')) as HTMLInputElement;
    const addButton = (await screen.findByText('Add new relay URL')) as HTMLButtonElement;

    await waitFor(() => expect(relayInput.disabled).toBe(true));
    expect(addButton.disabled).toBe(true);
    expect(screen.getByText('Stop the operator to edit relay URLs. Changes apply on next start.')).toBeTruthy();
  });

  it('disables relay URL editing while the operator is starting', async () => {
    let resolveStart: (() => void) | undefined;
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_compute_node') {
        return new Promise<void>((resolve) => {
          resolveStart = resolve;
        });
      }
      return mockInitialCommand(command);
    });

    render(<App />);
    const startOperatorButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    fireEvent.click(startOperatorButton);

    const relayInput = (await screen.findByLabelText('Relay URL 1')) as HTMLInputElement;
    const addButton = (await screen.findByText('Add new relay URL')) as HTMLButtonElement;
    await waitFor(() => expect(relayInput.disabled).toBe(true));
    expect(addButton.disabled).toBe(true);

    resolveStart?.();
  });

  it('ignores a stale start rejection after a replacement operator is healthy', async () => {
    let rejectFirstStart: ((error: Error) => void) | undefined;
    invokeMock.mockImplementation((command: string, args?: unknown) => {
      if (command === 'start_compute_node') {
        const startCalls = invokeMock.mock.calls.filter(([called]) => called === 'start_compute_node').length;
        if (startCalls === 1) {
          return new Promise<void>((_resolve, reject) => {
            rejectFirstStart = reject;
          });
        }
        return Promise.resolve();
      }
      return mockInitialCommand(command, args);
    });

    render(<App />);
    const startOperatorButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    fireEvent.click(startOperatorButton);

    await waitFor(() => expect(eventHandlers.has('compute_node_event')).toBe(true));
    eventHandlers.get('compute_node_event')?.({
      payload: {
        type: 'error',
        operator_session_id: 'session-1',
        sequence: 1,
        running: false,
        registered: false,
        relay_runtime_state: 'failed',
        worker_state: 'failed',
        last_error: 'first startup failed',
      },
    });
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    fireEvent.click(startOperatorButton);
    eventHandlers.get('compute_node_event')?.({
      payload: {
        type: 'started',
        operator_session_id: 'session-2',
        sequence: 1,
        running: true,
        registered: true,
        relay_runtime_state: 'ready',
        worker_state: 'ready',
        active_relay_url: 'https://token.place',
        registered_relay_count: 1,
      },
    });

    await waitFor(() => expect(screen.getByText(/Registered:/).textContent).toContain('yes'));
    rejectFirstStart?.(new Error('late session-1 rejection'));

    await waitFor(() => expect(screen.getByText(/Worker state:/).textContent).toContain('ready'));
    expect(screen.getByText(/Registered:/).textContent).toContain('yes');
    expect(screen.queryByText(/late session-1 rejection/)).toBeNull();
    expect(screen.getByText(/Last error:/).textContent).not.toContain('late session-1 rejection');
  });

  it('saves normalized relay_base_urls and first-url relay_base_url compatibility', async () => {
    render(<App />);
    const relayInput = (await screen.findByLabelText('Relay URL 1')) as HTMLInputElement;
    fireEvent.change(relayInput, { target: { value: ' https://staging.token.place ' } });

    await waitFor(() =>
      expect(
        invokeMock.mock.calls.some(
          ([command, args]) =>
            command === 'save_config' &&
            args?.config?.relay_base_url === 'https://staging.token.place' &&
            args?.config?.relay_base_urls?.[0] === 'https://staging.token.place'
        )
      ).toBe(true)
    );
  });

  it('starts the operator with the normalized configured relay list and primary relay URL', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'load_config') {
        return Promise.resolve({
          model_path: '/tmp/model.gguf',
          relay_base_url: 'https://legacy.example',
          relay_base_urls: [
            ' https://token.place ',
            'https://staging.token.place',
            'https://token.place',
            '',
          ],
          preferred_mode: 'auto',
        });
      }
      return mockInitialCommand(command);
    });

    render(<App />);
    const startOperatorButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    fireEvent.click(startOperatorButton);

    await waitFor(() =>
      expect(
        invokeMock.mock.calls.some(
          ([command, args]) =>
            command === 'start_compute_node' &&
            args?.request?.relay_base_url === 'https://token.place' &&
            args?.request?.context_tier === '8k-fast' &&
            JSON.stringify(args?.request?.relay_base_urls) ===
              JSON.stringify(['https://token.place', 'https://staging.token.place'])
        )
      ).toBe(true)
    );
  });


  it('persists 64k context tier while stopped, displays selected window, and starts by stable profile ID only', async () => {
    let startResolve: (() => void) | null = null;
    invokeMock.mockImplementation((command: string, args?: unknown) => {
      if (command === 'start_compute_node') {
        return new Promise<void>((resolve) => {
          startResolve = resolve;
        });
      }
      return mockInitialCommand(command, args);
    });

    render(<App />);
    const contextSelect = (await screen.findByLabelText('Context tier')) as HTMLSelectElement;
    await waitFor(() => expect(contextSelect.disabled).toBe(false));

    fireEvent.change(contextSelect, { target: { value: '64k-full' } });
    await waitFor(() =>
      expect(screen.getByText(/Context window:/).textContent).toContain('65536')
    );
    await waitFor(() =>
      expect(
        invokeMock.mock.calls.some(
          ([command, args]) =>
            command === 'save_config' &&
            args?.config?.context_tier === '64k-full'
        )
      ).toBe(true)
    );

    fireEvent.click((await screen.findByText('Start operator')) as HTMLButtonElement);
    expect(contextSelect.disabled).toBe(true);
    await waitFor(() =>
      expect(
        invokeMock.mock.calls.some(
          ([command, args]) =>
            command === 'start_compute_node' &&
            args?.request?.context_tier === '64k-full' &&
            args?.request?.n_ctx === undefined &&
            args?.request?.context_window_tokens === undefined
        )
      ).toBe(true)
    );

    startResolve?.();
  });

  it.each([
    ['starting', { running: false, worker_state: 'starting', relay_runtime_state: 'starting' }, true],
    ['warming', { running: false, worker_state: 'warming', warm_load_state: 'warming' }, true],
    ['running', { running: true, worker_state: 'ready', relay_runtime_state: 'ready' }, true],
    ['stopping', { running: false, worker_state: 'stopping', relay_runtime_state: 'stopping' }, true],
    ['recovering', { running: false, worker_state: 'recovering', relay_runtime_state: 'recovering' }, true],
    ['failed', { running: false, worker_state: 'failed', relay_runtime_state: 'failed', warm_load_state: 'failed' }, false],
  ])('sets context tier disabled=%s while operator is %s', async (_label, statusOverrides, expectedDisabled) => {
    mockInitialComputeStatus(statusOverrides);

    render(<App />);
    const contextSelect = (await screen.findByLabelText('Context tier')) as HTMLSelectElement;

    await waitFor(() => expect(contextSelect.disabled).toBe(expectedDisabled));
  });

  it('renders worker lifecycle fields and ignores stale worker generations', async () => {
    render(<App />);
    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    await waitFor(() => expect(screen.getByText(/Worker state:/).textContent).toContain('stopped'));

    computeHandler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        active_relay_url: 'https://token.place',
        relay_runtime_state: 'ready',
        worker_state: 'ready',
        worker_generation: 4,
        worker_restart_count: 1,
        worker_alive: true,
        last_worker_error_code: null,
        operator_session_id: 'session-1',
        sequence: 1,
      },
    });
    await waitFor(() => expect(screen.getByText(/Worker state:/).textContent).toContain('ready'));
    expect(screen.getByText(/Worker alive:/).textContent).toContain('yes');
    expect(screen.getByText(/Worker generation:/).textContent).toContain('4');
    expect(screen.getByText(/Worker restart count:/).textContent).toContain('1');

    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: false,
        relay_runtime_state: 'failed',
        worker_state: 'failed',
        worker_generation: 3,
        worker_restart_count: 9,
        worker_alive: false,
        last_worker_error_code: 'stale_failure',
        operator_session_id: 'session-1',
        sequence: 2,
      },
    });
    expect(screen.getByText(/Worker state:/).textContent).toContain('ready');
    expect(screen.queryByText('stale_failure')).toBeNull();

    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: false,
        relay_runtime_state: 'recovering',
        worker_state: 'recovering',
        worker_generation: 5,
        worker_restart_count: 2,
        worker_alive: false,
        last_worker_error_code: 'worker_dead',
        operator_session_id: 'session-1',
        sequence: 3,
      },
    });
    await waitFor(() => expect(screen.getByText(/Worker state:/).textContent).toContain('recovering'));
    expect(screen.getByText(/Worker alive:/).textContent).toContain('no');
    expect(screen.getByText(/Worker restart count:/).textContent).toContain('2');
    expect(screen.getByText(/Last worker error code:/).textContent).toContain('worker_dead');

    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        relay_runtime_state: 'failed',
        worker_state: 'failed',
        worker_generation: 5,
        worker_restart_count: 3,
        worker_alive: false,
        last_worker_error_code: 'fatal_worker_exit',
        operator_session_id: 'session-1',
        sequence: 4,
      },
    });
    await waitFor(() => expect(screen.getByText(/Worker state:/).textContent).toContain('failed'));
    expect(screen.getByText(/Worker restart count:/).textContent).toContain('3');
    expect(screen.getByText(/Last worker error code:/).textContent).toContain('fatal_worker_exit');
  });


  it('renders provisioning started event fields and locks controls', async () => {
    render(<App />);

    const startButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    const stopButton = (await screen.findByText('Stop operator')) as HTMLButtonElement;
    const inspectButton = (await screen.findByText('Open debug log')) as HTMLButtonElement;
    const handler = eventHandlers.get('compute_node_event');
    expect(handler).toBeTruthy();

    handler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        relay_runtime_state: 'provisioning',
        runtime_provisioning_state: 'provisioning',
        startup_phase: 'cuda_build',
        startup_elapsed_ms: 5100,
        startup_deadline_ms: 300000,
        worker_state: 'provisioning',
        worker_alive: false,
        log_file_path: 'C:/Users/alice/AppData/Local/token.place/operator.log',
        readiness_diagnostics: { startup_phase: 'cuda_build' },
        operator_session_id: 'provisioning-session',
        sequence: 1,
      },
    });

    await waitFor(() => expect(screen.getByText(/Running:/).textContent).toContain('yes'));
    expect(screen.getByText(/Worker alive:/).textContent).toContain('no');
    expect(screen.getByText(/Provisioning state:/).textContent).toContain('provisioning');
    expect(screen.getByText(/Startup phase:/).textContent).toContain('cuda_build');
    expect(screen.getByText(/Startup elapsed:/).textContent).toContain('5100');
    expect(screen.getByText(/Startup elapsed:/).textContent).toContain('/ 300000 ms');
    expect(screen.getByText(/Operator debug log:/).textContent).toContain('C:/Users/alice/AppData/Local/token.place/operator.log');
    expect(screen.getByText(/Readiness diagnostics:/).textContent).toContain('startup_phase=cuda_build');
    expect(stopButton.disabled).toBe(false);
    expect(startButton.disabled).toBe(true);
    expect(inspectButton.disabled).toBe(false);
  });

  it('hides unknown startup deadline and clears stale provisioning fields after live, error, and stopped events', async () => {
    render(<App />);

    const handler = eventHandlers.get('compute_node_event');
    expect(handler).toBeTruthy();

    handler?.({
      payload: {
        type: 'started',
        running: true,
        registered: false,
        relay_runtime_state: 'provisioning',
        runtime_provisioning_state: 'provisioning',
        startup_phase: 'dependency_check',
        startup_elapsed_ms: 0,
        startup_deadline_ms: null,
        worker_state: 'provisioning',
        worker_alive: false,
        operator_session_id: 'cleanup-session',
        sequence: 1,
      },
    });

    await waitFor(() => expect(screen.getByText(/Provisioning state:/).textContent).toContain('provisioning'));
    expect(screen.getByText(/Startup elapsed:/).textContent).toContain('0 ms');
    expect(screen.getByText(/Startup elapsed:/).textContent).not.toContain('/ 0 ms');

    handler?.({
      payload: {
        type: 'status',
        running: true,
        registered: true,
        relay_runtime_state: 'ready',
        worker_state: 'ready',
        worker_alive: true,
        operator_session_id: 'cleanup-session',
        sequence: 2,
      },
    });

    await waitFor(() => expect(screen.getByText(/Provisioning state:/).textContent).toContain('idle'));
    expect(screen.getByText(/Startup phase:/).textContent).toContain('none');
    expect(screen.getByText(/Startup elapsed:/).textContent).not.toContain('/ 0 ms');

    handler?.({
      payload: {
        type: 'started',
        running: true,
        relay_runtime_state: 'provisioning',
        runtime_provisioning_state: 'provisioning',
        startup_phase: 'runtime_install',
        startup_elapsed_ms: 2500,
        startup_deadline_ms: 300000,
        worker_state: 'provisioning',
        worker_alive: false,
        operator_session_id: 'cleanup-session',
        sequence: 3,
      },
    });
    await waitFor(() => expect(screen.getByText(/Provisioning state:/).textContent).toContain('provisioning'));

    handler?.({
      payload: {
        type: 'error',
        message: 'failed',
        operator_session_id: 'cleanup-session',
        sequence: 4,
      },
    });
    await waitFor(() => expect(screen.getByText(/Provisioning state:/).textContent).toContain('idle'));

    handler?.({
      payload: {
        type: 'started',
        running: true,
        relay_runtime_state: 'provisioning',
        runtime_provisioning_state: 'provisioning',
        startup_phase: 'runtime_install',
        startup_elapsed_ms: 2500,
        startup_deadline_ms: 300000,
        worker_state: 'provisioning',
        worker_alive: false,
        operator_session_id: 'cleanup-session',
        sequence: 5,
      },
    });
    await waitFor(() => expect(screen.getByText(/Provisioning state:/).textContent).toContain('provisioning'));

    handler?.({
      payload: {
        type: 'stopped',
        operator_session_id: 'cleanup-session',
        sequence: 6,
      },
    });
    await waitFor(() => expect(screen.getByText(/Provisioning state:/).textContent).toContain('idle'));
  });

  it('re-enables context tier and relay controls immediately after successful Stop Operator', async () => {
    mockInitialComputeStatus(
      {
        running: true,
        registered: true,
        active_relay_url: 'https://token.place',
        relay_runtime_state: 'ready',
        warm_load_state: 'ready',
        worker_state: 'ready',
        worker_alive: true,
        registered_relay_count: 2,
        registered_relay_urls: ['https://token.place', 'https://staging.token.place'],
        active_relay_urls: ['https://token.place', 'https://staging.token.place'],
        operator_session_id: 'session-1',
        sequence: 2,
      },
      {
        relay_base_urls: ['https://token.place', 'https://staging.token.place'],
      }
    );

    render(<App />);
    const contextSelect = (await screen.findByLabelText('Context tier')) as HTMLSelectElement;
    const relayInput = (await screen.findByLabelText('Relay URL 1')) as HTMLInputElement;
    const addRelayButton = (await screen.findByText('Add new relay URL')) as HTMLButtonElement;
    const deleteSecondRelayButton = (await screen.findByLabelText(
      'Delete relay URL 2'
    )) as HTMLButtonElement;
    const stopOperatorButton = (await screen.findByText('Stop operator')) as HTMLButtonElement;

    await waitFor(() => expect(contextSelect.disabled).toBe(true));
    expect(relayInput.disabled).toBe(true);
    expect(addRelayButton.disabled).toBe(true);
    expect(deleteSecondRelayButton.disabled).toBe(true);

    fireEvent.click(stopOperatorButton);

    await waitFor(() => expect(contextSelect.disabled).toBe(false));
    expect(relayInput.disabled).toBe(false);
    expect(addRelayButton.disabled).toBe(false);
    expect(deleteSecondRelayButton.disabled).toBe(false);
    expect(screen.getByText(/Running:/).textContent).toContain('no');
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('stopped');

    fireEvent.change(contextSelect, { target: { value: '64k-full' } });
    const startOperatorButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    fireEvent.click(startOperatorButton);

    await waitFor(() =>
      expect(
        invokeMock.mock.calls.some(
          ([command, args]) =>
            command === 'start_compute_node' && args?.request?.context_tier === '64k-full'
        )
      ).toBe(true)
    );
  });

  it('treats stopped events with omitted fields as terminal stopped state', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: true,
      active_relay_url: 'https://token.place',
      relay_runtime_state: 'ready',
      warm_load_state: 'ready',
      worker_state: 'ready',
      worker_alive: true,
      operator_session_id: 'session-1',
      sequence: 2,
    });

    render(<App />);
    const contextSelect = (await screen.findByLabelText('Context tier')) as HTMLSelectElement;
    await waitFor(() => expect(contextSelect.disabled).toBe(true));

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'stopped',
        operator_session_id: 'session-1',
        sequence: 3,
      },
    });

    await waitFor(() => expect(contextSelect.disabled).toBe(false));
    expect(screen.getByText(/Running:/).textContent).toContain('no');
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Worker state:/).textContent).toContain('stopped');
    expect(screen.getByText(/Worker alive:/).textContent).toContain('no');
  });

  it('forces stopped worker fields even when stopped events include ready lifecycle values', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: true,
      active_relay_url: 'https://token.place',
      relay_runtime_state: 'ready',
      warm_load_state: 'ready',
      worker_state: 'ready',
      worker_alive: true,
      operator_session_id: 'session-1',
      sequence: 2,
    });

    render(<App />);
    const contextSelect = (await screen.findByLabelText('Context tier')) as HTMLSelectElement;
    const relayInput = (await screen.findByLabelText('Relay URL 1')) as HTMLInputElement;
    const addRelayButton = (await screen.findByText('Add new relay URL')) as HTMLButtonElement;
    await waitFor(() => expect(contextSelect.disabled).toBe(true));
    expect(relayInput.disabled).toBe(true);
    expect(addRelayButton.disabled).toBe(true);

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'stopped',
        running: true,
        registered: true,
        active_relay_url: 'https://token.place',
        relay_runtime_state: 'ready',
        warm_load_state: 'ready',
        worker_state: 'ready',
        worker_alive: true,
        registered_relay_count: 1,
        registered_relay_urls: ['https://token.place'],
        active_relay_urls: ['https://token.place'],
        relay_statuses: [{ relay_url: 'https://token.place', registered: true }],
        operator_session_id: 'session-1',
        sequence: 3,
      },
    });

    await waitFor(() => expect(contextSelect.disabled).toBe(false));
    expect(relayInput.disabled).toBe(false);
    expect(addRelayButton.disabled).toBe(false);
    expect(screen.getByText(/Running:/).textContent).toContain('no');
    expect(screen.getByText(/Registered:/).textContent).toContain('no');
    expect(screen.getByText(/Relay runtime state:/).textContent).toContain('stopped');
    expect(screen.getByText(/Worker state:/).textContent).toContain('stopped');
    expect(screen.getByText(/Worker alive:/).textContent).toContain('no');
    expect(screen.queryByText(/Per-relay status/)).toBeNull();
  });

  it('preserves relay runtime state when status events only include warm-load state', async () => {
    mockInitialComputeStatus({
      running: false,
      registered: false,
      relay_runtime_state: 'failed',
      warm_load_state: 'failed',
      worker_state: 'failed',
      worker_alive: false,
      operator_session_id: 'session-1',
      sequence: 2,
    });

    render(<App />);
    await screen.findByText(/Relay runtime state:/);

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'status',
        warm_load_state: 'ready',
        operator_session_id: 'session-1',
        sequence: 3,
      },
    });

    await waitFor(() => expect(screen.getByText(/Relay runtime state:/).textContent).toContain('failed'));
  });

  it('re-enables context tier after pre-registration failure event', async () => {
    render(<App />);
    const contextSelect = (await screen.findByLabelText('Context tier')) as HTMLSelectElement;
    const startButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startButton.disabled).toBe(false));

    fireEvent.click(startButton);
    await waitFor(() => expect(contextSelect.disabled).toBe(true));

    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();
    computeHandler?.({
      payload: {
        type: 'error',
        running: false,
        registered: false,
        relay_runtime_state: 'failed',
        warm_load_state: 'failed',
        worker_state: 'failed',
        worker_alive: false,
        last_error: 'registration failed before ready',
        operator_session_id: 'session-failed',
        sequence: 1,
      },
    });

    await waitFor(() => expect(contextSelect.disabled).toBe(false));
    expect(screen.getByText(/Last error:/).textContent).toContain('registration failed before ready');
  });

  it('keeps stopped controls enabled when stale previous-session running events arrive after stop', async () => {
    mockInitialComputeStatus({
      running: true,
      registered: true,
      relay_runtime_state: 'ready',
      worker_state: 'ready',
      operator_session_id: 'session-2',
      sequence: 5,
    });

    render(<App />);
    const contextSelect = (await screen.findByLabelText('Context tier')) as HTMLSelectElement;
    const computeHandler = eventHandlers.get('compute_node_event');
    expect(computeHandler).toBeTruthy();

    computeHandler?.({
      payload: {
        type: 'stopped',
        running: false,
        registered: false,
        relay_runtime_state: 'stopped',
        worker_state: 'stopped',
        operator_session_id: 'session-2',
        sequence: 6,
      },
    });
    await waitFor(() => expect(contextSelect.disabled).toBe(false));

    computeHandler?.({
      payload: {
        type: 'status',
        running: true,
        registered: true,
        relay_runtime_state: 'ready',
        worker_state: 'ready',
        operator_session_id: 'session-1',
        sequence: 100,
      },
    });

    await waitFor(() => expect(contextSelect.disabled).toBe(false));
    expect(screen.getByText(/Running:/).textContent).toContain('no');
  });

  it('leaves the running state intact and surfaces safe error when Stop Operator fails', async () => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'stop_compute_node') {
        return Promise.reject(new Error('stop failed safely'));
      }
      if (command === 'get_compute_node_status') {
        return Promise.resolve({
          running: true,
          registered: true,
          active_relay_url: 'https://token.place',
          requested_mode: 'auto',
          effective_mode: 'cpu',
          backend_available: 'unknown',
          backend_selected: 'cpu',
          backend_used: 'cpu',
          fallback_reason: null,
          model_path: '/tmp/model.gguf',
          last_error: null,
          relay_runtime_state: 'ready',
          worker_state: 'ready',
        });
      }
      return mockInitialCommand(command);
    });

    render(<App />);
    const contextSelect = (await screen.findByLabelText('Context tier')) as HTMLSelectElement;
    const stopButton = (await screen.findByText('Stop operator')) as HTMLButtonElement;
    await waitFor(() => expect(contextSelect.disabled).toBe(true));

    fireEvent.click(stopButton);

    await waitFor(() => expect(screen.getByText(/Error:/).textContent).toContain('stop failed safely'));
    expect(screen.getByText(/Running:/).textContent).toContain('yes');
    expect(contextSelect.disabled).toBe(true);
  });

});

describe('desktop Python runtime error normalization', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    invokeMock.mockReset();
    listenMock.mockReset();
    eventHandlers.clear();
    listenMock.mockImplementation((event: string, handler: unknown) => {
      eventHandlers.set(event, handler as (evt: { payload: Record<string, unknown> }) => void);
      return Promise.resolve(() => {});
    });
  });



  it.each([
    'desktop_python_runtime_missing: bundled runtime unavailable',
    'desktop_python_runtime_invalid: bundled runtime failed metadata probe',
  ])('keeps Start operator enabled after mount-time bridge inspection failure: %s', async (bridgeError) => {
    invokeMock.mockImplementation((command: string) => {
      if (command === 'detect_backend') return Promise.resolve({ platform_label: 'windows-x64', preferred_mode: 'gpu', available_backend: 'cuda', availability_label: 'CUDA-capable platform (Windows x64)' });
      if (command === 'load_config') return Promise.resolve({ model_path: 'C:\\Models\\qwen.gguf', relay_base_url: 'https://token.place', preferred_mode: 'gpu' });
      if (command === 'get_compute_node_status') return Promise.resolve({ running: false, registered: false, active_relay_url: '', requested_mode: 'gpu', effective_mode: null, backend_available: 'cuda', backend_selected: null, backend_used: null, fallback_reason: null, model_path: '', last_error: null, relay_runtime_state: 'idle', runtime_provisioning_state: 'idle', startup_phase: null });
      if (command === 'inspect_model_artifact') return Promise.reject(new Error(bridgeError));
      return Promise.resolve(undefined);
    });

    render(<App />);
    await screen.findByText(/The bundled token\.place runtime is missing or damaged/);
    expect(document.body.textContent ?? '').toContain(bridgeError.split(':')[0]);
    const startInferenceButton = (await screen.findByText('Start local inference')) as HTMLButtonElement;
    const downloadButton = (await screen.findByText('Download')) as HTMLButtonElement;
    const startOperatorButton = (await screen.findByText('Start operator')) as HTMLButtonElement;
    await waitFor(() => expect(startOperatorButton.disabled).toBe(false));
    expect(startInferenceButton.disabled).toBe(true);
    expect(downloadButton.disabled).toBe(true);

    fireEvent.click(startOperatorButton);
    await waitFor(() => expect(invokeMock.mock.calls.some(([command]) => command === 'start_compute_node')).toBe(true));
  });

  it('hides raw xcode-select launcher diagnostics and disables Python-dependent actions', async () => {
    const rawFailure = "no usable Python 3 interpreter found for desktop Python subprocess (consulted override env var: TOKEN_PLACE_SIDECAR_PYTHON); tried: python3 -> status=1 stdout='' stderr='xcode-select: note: No developer tools were found, requesting install. If developer tools are located at a non-default location on disk, use sudo xcode-select --switch /Applications/Xcode.app. /Users/daniel/private'; python -> spawn failed: missing";
    invokeMock.mockImplementation((command: string) => {
      if (command === 'start_inference') return Promise.reject(new Error(rawFailure));
      if (command === 'detect_backend') return Promise.resolve({ platform_label: 'macos', preferred_mode: 'auto', available_backend: 'metal', availability_label: 'Metal-capable platform (Apple Silicon)' });
      if (command === 'load_config') return Promise.resolve({ model_path: '/tmp/model.gguf', relay_base_url: 'https://token.place', preferred_mode: 'auto' });
      if (command === 'get_compute_node_status') return Promise.resolve({ running: false, registered: false, active_relay_url: '', requested_mode: 'auto', effective_mode: 'cpu', backend_available: 'unknown', backend_selected: 'cpu', backend_used: 'cpu', fallback_reason: null, model_path: '', last_error: null });
      return Promise.resolve({ canonical_family_url: 'https://example.test/models', filename: 'model.gguf', url: 'https://example.test/model.gguf', models_dir: '/tmp', resolved_model_path: '/tmp/model.gguf', exists: true, size_bytes: 1 });
    });

    render(<App />);
    const promptArea = (await screen.findByText('Prompt')).parentElement?.querySelector('textarea');
    fireEvent.change(promptArea as HTMLTextAreaElement, { target: { value: 'hello' } });
    const startInferenceButton = (await screen.findByText('Start local inference')) as HTMLButtonElement;
    await waitFor(() => expect(startInferenceButton.disabled).toBe(false));
    fireEvent.click(startInferenceButton);

    await screen.findByText(/The bundled token\.place runtime is missing or damaged/);
    const body = document.body.textContent ?? '';
    expect(body).toContain('Diagnostic code: desktop_python_runtime_invalid');
    expect(body).not.toContain('xcode-select');
    expect(body).not.toContain('sudo');
    expect(body).not.toContain('Xcode.app');
    expect(body).not.toContain('TOKEN_PLACE_SIDECAR_PYTHON');
    expect(body).not.toContain('/Users/daniel');
    expect(((await screen.findByText('Start local inference')) as HTMLButtonElement).disabled).toBe(true);
    expect(((await screen.findByText('Start operator')) as HTMLButtonElement).disabled).toBe(false);
    expect(((await screen.findByText('Download')) as HTMLButtonElement).disabled).toBe(true);
  });
});
