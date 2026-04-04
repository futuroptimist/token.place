import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { useEffect, useMemo, useState } from 'react';

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

interface SidecarEvent {
  request_id: string;
  type: string;
  text?: string;
  code?: string;
  message?: string;
}

export function App() {
  const [backend, setBackend] = useState<BackendInfo | null>(null);
  const [config, setConfig] = useState<DesktopConfig>({
    model_path: '',
    relay_base_url: 'http://127.0.0.1:5010',
    preferred_mode: 'auto',
  });
  const [prompt, setPrompt] = useState('');
  const [output, setOutput] = useState('');
  const [requestId, setRequestId] = useState<string>('');
  const [status, setStatus] = useState<UiState>('idle');
  const [error, setError] = useState('');

  useEffect(() => {
    invoke<BackendInfo>('detect_backend').then(setBackend);
    invoke<DesktopConfig>('load_config').then(setConfig);
    const unlisten = listen<SidecarEvent>('inference_event', (evt) => {
      const payload = evt.payload;
      if (payload.request_id !== requestId) {
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
  }, [requestId]);

  const canStart = useMemo(
    () => Boolean(config.model_path.trim()) && Boolean(prompt.trim()) && status !== 'starting' && status !== 'streaming',
    [config.model_path, prompt, status]
  );

  const saveConfig = async (next: DesktopConfig) => {
    setConfig(next);
    await invoke('save_config', { config: next });
  };

  const startInference = async () => {
    setOutput('');
    setError('');
    setStatus('starting');
    const nextRequestId = crypto.randomUUID();
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
    if (!requestId) return;
    await invoke('cancel_inference', { requestId });
  };

  const forwardEncrypted = async () => {
    await invoke('encrypt_and_forward', {
      relayBaseUrl: config.relay_base_url,
      finalOutput: output,
    });
  };

  return (
    <main style={{ maxWidth: 820, margin: '20px auto', fontFamily: 'sans-serif' }}>
      <h1>token.place desktop (Tauri MVP)</h1>
      <p>Detected backend: <strong>{backend?.display_label ?? 'loading...'}</strong></p>
      <label>Model GGUF path</label>
      <input value={config.model_path} style={{ width: '100%' }} onChange={(e) => saveConfig({ ...config, model_path: e.target.value })} />

      <label style={{ display: 'block', marginTop: 12 }}>Compute mode</label>
      <select value={config.preferred_mode} onChange={(e) => saveConfig({ ...config, preferred_mode: e.target.value as BackendMode })}>
        <option value="auto">Auto ({backend?.display_label ?? '...'})</option>
        <option value="cpu">CPU fallback</option>
      </select>

      <label style={{ display: 'block', marginTop: 12 }}>Prompt</label>
      <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={6} style={{ width: '100%' }} />

      <div style={{ marginTop: 10, display: 'flex', gap: 8 }}>
        <button disabled={!canStart} onClick={startInference}>Start inference</button>
        <button disabled={status !== 'starting' && status !== 'streaming'} onClick={cancelInference}>Cancel</button>
        <button disabled={!output} onClick={forwardEncrypted}>Encrypt + forward output</button>
      </div>

      <p>Status: <strong>{status}</strong></p>
      {error && <p style={{ color: 'crimson' }}>Error: {error}</p>}
      <pre style={{ whiteSpace: 'pre-wrap', padding: 12, border: '1px solid #ddd' }}>{output}</pre>

      <label style={{ display: 'block', marginTop: 12 }}>Relay URL</label>
      <input value={config.relay_base_url} style={{ width: '100%' }} onChange={(e) => saveConfig({ ...config, relay_base_url: e.target.value })} />
    </main>
  );
}
