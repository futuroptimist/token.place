import { useEffect, useMemo, useState } from 'react';
import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';

type ComputeMode = 'auto' | 'metal' | 'cuda' | 'cpu';
type UiState = 'idle' | 'starting' | 'streaming' | 'canceled' | 'completed' | 'failed';

type AppConfig = {
  model_path: string;
  relay_base_url: string;
  preferred_compute_mode: ComputeMode;
};

type BackendInfo = {
  preferred_backend: string;
  display_label: string;
  platform: string;
};

type SidecarEvent = {
  type: 'started' | 'token' | 'done' | 'error' | 'canceled';
  text?: string;
  code?: string;
  message?: string;
};

const defaultConfig: AppConfig = {
  model_path: '',
  relay_base_url: 'http://127.0.0.1:5001',
  preferred_compute_mode: 'auto'
};

export function App() {
  const [config, setConfig] = useState<AppConfig>(defaultConfig);
  const [backend, setBackend] = useState<BackendInfo | null>(null);
  const [prompt, setPrompt] = useState('');
  const [output, setOutput] = useState('');
  const [status, setStatus] = useState<UiState>('idle');
  const [errorMessage, setErrorMessage] = useState('');
  const [forwardResult, setForwardResult] = useState('');

  useEffect(() => {
    let unlisten: UnlistenFn | null = null;

    const start = async () => {
      const loaded = await invoke<AppConfig>('load_desktop_config');
      setConfig(loaded);
      setBackend(await invoke<BackendInfo>('detect_backend', { preferredMode: loaded.preferred_compute_mode }));

      unlisten = await listen<SidecarEvent>('inference-event', (event) => {
        const payload = event.payload;
        if (payload.type === 'started') {
          setStatus('streaming');
          return;
        }
        if (payload.type === 'token') {
          setOutput((prev) => `${prev}${payload.text ?? ''}`);
          return;
        }
        if (payload.type === 'done') {
          setStatus('completed');
          return;
        }
        if (payload.type === 'canceled') {
          setStatus('canceled');
          return;
        }
        if (payload.type === 'error') {
          setStatus('failed');
          setErrorMessage(payload.message ?? payload.code ?? 'Inference failed');
        }
      });
    };

    void start();
    return () => {
      if (unlisten) {
        void unlisten();
      }
    };
  }, []);

  const canStart = useMemo(() => {
    return Boolean(config.model_path && prompt.trim()) && status !== 'starting' && status !== 'streaming';
  }, [config.model_path, prompt, status]);

  const saveConfig = async (next: AppConfig) => {
    setConfig(next);
    await invoke('save_desktop_config', { nextConfig: next });
    setBackend(await invoke<BackendInfo>('detect_backend', { preferredMode: next.preferred_compute_mode }));
  };

  const chooseModel = async () => {
    const selected = await open({
      multiple: false,
      filters: [{ name: 'GGUF model', extensions: ['gguf'] }]
    });
    if (typeof selected === 'string') {
      await saveConfig({ ...config, model_path: selected });
    }
  };

  const startInference = async () => {
    setStatus('starting');
    setOutput('');
    setErrorMessage('');
    setForwardResult('');
    await invoke('start_inference', {
      request: {
        modelPath: config.model_path,
        prompt,
        preferredMode: config.preferred_compute_mode
      }
    });
  };

  const cancelInference = async () => {
    await invoke('cancel_inference');
  };

  const forwardEncrypted = async () => {
    const result = await invoke<string>('forward_output_encrypted', {
      relayBaseUrl: config.relay_base_url,
      output
    });
    setForwardResult(result);
  };

  return (
    <main className="container">
      <h1>token.place desktop MVP</h1>

      <label>Model file (GGUF)</label>
      <div className="row">
        <input value={config.model_path} readOnly />
        <button onClick={chooseModel}>Choose…</button>
      </div>

      <label>Compute mode</label>
      <select
        value={config.preferred_compute_mode}
        onChange={(e) => void saveConfig({ ...config, preferred_compute_mode: e.target.value as ComputeMode })}
      >
        <option value="auto">auto</option>
        <option value="cpu">cpu</option>
      </select>

      <p className="meta">
        Preferred backend: <strong>{backend?.display_label ?? 'loading…'}</strong>
      </p>

      <label>Relay base URL</label>
      <input
        value={config.relay_base_url}
        onChange={(e) => setConfig({ ...config, relay_base_url: e.target.value })}
        onBlur={() => void saveConfig(config)}
      />

      <label>Prompt</label>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={6} />

      <div className="row">
        <button disabled={!canStart} onClick={() => void startInference()}>
          Start inference
        </button>
        <button disabled={status !== 'starting' && status !== 'streaming'} onClick={() => void cancelInference()}>
          Cancel
        </button>
        <button disabled={!output.trim()} onClick={() => void forwardEncrypted()}>
          Encrypt + forward
        </button>
      </div>

      <p className="meta">State: {status}</p>
      {errorMessage ? <p className="error">{errorMessage}</p> : null}
      {forwardResult ? <p className="meta">Forward result: {forwardResult}</p> : null}

      <label>Output</label>
      <pre>{output}</pre>
    </main>
  );
}
