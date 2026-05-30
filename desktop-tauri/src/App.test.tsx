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


  const mockInitialComputeStatus = (statusOverrides: Record<string, unknown>) => {
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
    expect(screen.getByText(/Last error:/).textContent).toContain('none');
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
});
