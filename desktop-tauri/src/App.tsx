import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';
import { useEffect, useMemo, useRef, useState } from 'react';

type UiState = 'idle' | 'starting' | 'streaming' | 'canceled' | 'completed' | 'failed';
type BackendMode = 'auto' | 'metal' | 'cuda' | 'cpu';

interface BackendInfo {
  platform_label: string;
  preferred_mode: BackendMode;
  display_label: string;
}

interface DesktopConfig {
  model_path: string;
  relay_base_url: string;
  preferred_mode: BackendMode;
}

interface ModelArtifactInfo {
  canonical_family_url: string;
  filename: string;
  url: string;
  models_dir: string;
  resolved_model_path: string;
  exists: boolean;
  size_bytes: number | null;
}

interface SidecarEvent {
  request_id: string;
  type: string;
  text?: string;
  code?: string;
  message?: string;
}

interface ComputeNodeStatus {
  state: string;
  registered: boolean;
  running: boolean;
  relay_url: string;
  backend_mode: string;
  model_path: string;
  last_error: string;
}

export function selectedModelPath(selection: string | string[] | null): string {
  if (typeof selection === 'string') {
    return selection;
  }
  if (Array.isArray(selection) && selection.length > 0 && typeof selection[0] === 'string') {
    return selection[0];
  }
  return '';
}

export function App() {
  const [backend, setBackend] = useState<BackendInfo | null>(null);
  const [config, setConfig] = useState<DesktopConfig>({
    model_path: '',
    relay_base_url: 'https://token.place',
    preferred_mode: 'auto',
  });
  const [prompt, setPrompt] = useState('');
  const [output, setOutput] = useState('');
  const [requestId, setRequestId] = useState<string>('');
  const [status, setStatus] = useState<UiState>('idle');
  const [artifact, setArtifact] = useState<ModelArtifactInfo | null>(null);
  const [isDownloadingModel, setIsDownloadingModel] = useState(false);
  const [error, setError] = useState('');
  const [isForwarding, setIsForwarding] = useState(false);
  const [operatorStatus, setOperatorStatus] = useState<ComputeNodeStatus>({
    state: 'stopped',
    registered: false,
    running: false,
    relay_url: '',
    backend_mode: 'unknown',
    model_path: '',
    last_error: '',
  });
  const saveTimerRef = useRef<number | null>(null);
  const requestIdRef = useRef('');

  useEffect(() => {
    invoke<BackendInfo>('detect_backend').then(setBackend).catch((e) => setError(String(e)));

    const initializeConfigAndArtifact = async () => {
      try {
        const loadedConfig = await invoke<DesktopConfig>('load_config');
        setConfig(loadedConfig);
        const status = await invoke<ComputeNodeStatus>('compute_node_status');
        setOperatorStatus(status);

        const info = await invoke<ModelArtifactInfo>('inspect_model_artifact');
        setArtifact(info);

        if (loadedConfig.model_path.trim()) {
          return;
        }

        const next = { ...loadedConfig, model_path: info.resolved_model_path };
        await invoke('save_config', { config: next });
        setConfig(next);
      } catch (e) {
        setError(String(e));
      }
    };

    initializeConfigAndArtifact();
  }, []);

  useEffect(() => {
    requestIdRef.current = requestId;
  }, [requestId]);

  useEffect(() => {
    const unlisten = listen<SidecarEvent>('inference_event', (evt) => {
      const payload = evt.payload;
      if (payload.request_id !== requestIdRef.current) {
        return;
      }
      if (payload.type === 'started') {
        setStatus('streaming');
      } else if (payload.type === 'token' && payload.text) {
        setOutput((prev) => prev + payload.text);
      } else if (payload.type === 'done') {
        setStatus('completed');
      } else if (payload.type === 'canceled') {
        setStatus('canceled');
      } else if (payload.type === 'error') {
        setStatus('failed');
        setError(payload.message ?? payload.code ?? 'unknown error');
      }
    });
    return () => {
      unlisten.then((f) => f());
    };
  }, []);

  useEffect(() => {
    const unlisten = listen<ComputeNodeStatus>('compute_node_status', (evt) => {
      setOperatorStatus(evt.payload);
    });
    return () => {
      unlisten.then((f) => f());
    };
  }, []);

  useEffect(() => {
    return () => {
      if (saveTimerRef.current !== null) {
        window.clearTimeout(saveTimerRef.current);
      }
    };
  }, []);

  const canStart = useMemo(
    () => Boolean(config.model_path.trim()) && Boolean(prompt.trim()) && status !== 'starting' && status !== 'streaming',
    [config.model_path, prompt, status]
  );

  const scheduleConfigSave = (next: DesktopConfig) => {
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
    }
    saveTimerRef.current = window.setTimeout(() => {
      invoke('save_config', { config: next }).catch((e) => setError(String(e)));
      saveTimerRef.current = null;
    }, 300);
  };

  const updateConfig = (next: DesktopConfig) => {
    setConfig(next);
    scheduleConfigSave(next);
  };

  const chooseModelPath = async () => {
    try {
      const selection = await open({
        multiple: false,
        directory: false,
        filters: [{ name: 'GGUF models', extensions: ['gguf'] }],
      });
      const path = selectedModelPath(selection);
      if (path) {
        updateConfig({ ...config, model_path: path });
      }
    } catch (e) {
      setError(String(e));
    }
  };

  const downloadModel = async () => {
    try {
      setIsDownloadingModel(true);
      setError('');
      const info = await invoke<ModelArtifactInfo>('download_model_artifact');
      setArtifact(info);
      setConfig((prev) => {
        const next = { ...prev, model_path: info.resolved_model_path };
        scheduleConfigSave(next);
        return next;
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setIsDownloadingModel(false);
    }
  };

  const startInference = async () => {
    setOutput('');
    setError('');
    setStatus('starting');
    const nextRequestId = crypto.randomUUID();
    requestIdRef.current = nextRequestId;
    setRequestId(nextRequestId);
    await invoke('start_inference', {
      request: {
        request_id: nextRequestId,
        model_path: config.model_path,
        prompt,
        mode: config.preferred_mode,
      },
    });
  };

  const cancelInference = async () => {
    if (!requestIdRef.current) return;
    await invoke('cancel_inference', { request_id: requestIdRef.current });
  };

  const forwardEncrypted = async () => {
    try {
      setIsForwarding(true);
      setError('');
      await invoke('encrypt_and_forward', {
        relay_base_url: config.relay_base_url,
        final_output: output,
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setIsForwarding(false);
    }
  };

  const startOperator = async () => {
    setError('');
    try {
      await invoke('start_compute_node', {
        request: {
          model_path: config.model_path,
          relay_base_url: config.relay_base_url,
          preferred_mode: config.preferred_mode,
        },
      });
    } catch (e) {
      setError(String(e));
    }
  };

  const stopOperator = async () => {
    setError('');
    try {
      await invoke('stop_compute_node');
      const status = await invoke<ComputeNodeStatus>('compute_node_status');
      setOperatorStatus(status);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <main style={{ maxWidth: 820, margin: '20px auto', fontFamily: 'sans-serif' }}>
      <h1>token.place desktop (Tauri MVP)</h1>
      <p>Detected backend: <strong>{backend?.display_label ?? 'loading...'}</strong></p>
      <section style={{ border: '1px solid #ddd', padding: 12, marginBottom: 12 }}>
        <h2 style={{ marginTop: 0 }}>Compute node operator (production path)</h2>
        <p style={{ marginBottom: 8 }}>
          Status: <strong>{operatorStatus.state}</strong> · Registered:{' '}
          <strong>{operatorStatus.registered ? 'yes' : 'no'}</strong>
        </p>
        <ul style={{ marginTop: 0 }}>
          <li>Active relay URL: <code>{operatorStatus.relay_url || config.relay_base_url}</code></li>
          <li>Backend mode: <code>{operatorStatus.backend_mode}</code></li>
          <li>Model path: <code>{operatorStatus.model_path || config.model_path || 'unset'}</code></li>
          <li>Last error: <code>{operatorStatus.last_error || 'none'}</code></li>
        </ul>
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" onClick={startOperator} disabled={!config.model_path || operatorStatus.running}>
            Start compute node
          </button>
          <button type="button" onClick={stopOperator} disabled={!operatorStatus.running}>
            Stop compute node
          </button>
        </div>
      </section>
      <label>Model GGUF path</label>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          value={config.model_path}
          style={{ width: '100%' }}
          onChange={(e) => updateConfig({ ...config, model_path: e.target.value })}
        />
        <button type="button" onClick={chooseModelPath}>Browse</button>
        <button type="button" onClick={downloadModel} disabled={isDownloadingModel}>
          {isDownloadingModel ? 'Downloading…' : 'Download'}
        </button>
      </div>
      {artifact && (
        <section style={{ marginTop: 8, fontSize: 14 }}>
          <div>
            Canonical model family:{' '}
            <a href={artifact.canonical_family_url} target="_blank" rel="noreferrer">
              {artifact.canonical_family_url}
            </a>
          </div>
          <div>Runtime GGUF filename: <code>{artifact.filename}</code></div>
          <div>
            Runtime GGUF source:{' '}
            <a href={artifact.url} target="_blank" rel="noreferrer">
              {artifact.url}
            </a>
          </div>
          <div>Runtime models directory: <code>{artifact.models_dir}</code></div>
          <div>Runtime resolved path: <code>{artifact.resolved_model_path}</code></div>
          <div>
            Downloaded: <strong>{artifact.exists ? 'yes' : 'no'}</strong>
            {artifact.size_bytes != null ? ` (${artifact.size_bytes.toLocaleString()} bytes)` : ''}
          </div>
        </section>
      )}

      <label style={{ display: 'block', marginTop: 12 }}>Compute mode</label>
      <select value={config.preferred_mode} onChange={(e) => updateConfig({ ...config, preferred_mode: e.target.value as BackendMode })}>
        <option value="auto">Auto ({backend?.display_label ?? '...'})</option>
        <option value="cpu">CPU fallback</option>
      </select>

      <label style={{ display: 'block', marginTop: 12 }}>Prompt (local smoke test)</label>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={6} style={{ width: '100%' }} />

      <div style={{ marginTop: 10, display: 'flex', gap: 8 }}>
        <button disabled={!canStart} onClick={startInference}>Start inference</button>
        <button disabled={status !== 'starting' && status !== 'streaming'} onClick={cancelInference}>Cancel</button>
        <button disabled={!output || isForwarding} onClick={forwardEncrypted}>Debug: encrypt + forward local output</button>
      </div>

      <p>Status: <strong>{status}</strong></p>
      {error && <p style={{ color: 'crimson' }}>Error: {error}</p>}
      <pre style={{ whiteSpace: 'pre-wrap', padding: 12, border: '1px solid #ddd' }}>{output}</pre>

      <label style={{ display: 'block', marginTop: 12 }}>Relay URL</label>
      <input value={config.relay_base_url} style={{ width: '100%' }} onChange={(e) => updateConfig({ ...config, relay_base_url: e.target.value })} />
    </main>
  );
}
