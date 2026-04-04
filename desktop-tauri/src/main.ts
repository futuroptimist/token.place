import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

type AppConfig = {
  modelPath: string;
  relayBaseUrl: string;
  computeMode: 'auto' | 'metal' | 'cuda' | 'cpu';
};

type BackendInfo = {
  preferredLabel: string;
  backend: 'metal' | 'cuda' | 'cpu';
  reason: string;
};

type InferenceEvent = {
  runId: string;
  type: 'started' | 'token' | 'done' | 'error' | 'canceled';
  text?: string;
  code?: string;
  message?: string;
};

const app = document.querySelector<HTMLDivElement>('#app');
if (!app) throw new Error('Missing app root');

app.innerHTML = `
  <h1>token.place desktop MVP</h1>
  <div class="panel">
    <div class="muted" id="backend"></div>
    <div class="row">
      <div>
        <label>Model path (GGUF)</label>
        <input id="modelPath" placeholder="/path/to/model.gguf" />
      </div>
      <div>
        <label>Compute mode</label>
        <select id="computeMode">
          <option value="auto">Auto</option>
          <option value="cpu">CPU fallback</option>
        </select>
      </div>
    </div>
    <label>Relay base URL (optional, for encrypt+forward)</label>
    <input id="relayBaseUrl" placeholder="http://127.0.0.1:5000" />
    <button id="saveConfig">Save config</button>
  </div>

  <div class="panel">
    <label>Prompt</label>
    <textarea id="prompt" rows="5"></textarea>
    <div class="row">
      <button id="start">Start inference</button>
      <button id="cancel">Cancel</button>
    </div>
    <div class="muted" id="status">idle</div>
    <pre id="output"></pre>
    <button id="forward">Encrypt + forward final output</button>
  </div>
`;

const el = {
  backend: document.getElementById('backend') as HTMLDivElement,
  modelPath: document.getElementById('modelPath') as HTMLInputElement,
  relayBaseUrl: document.getElementById('relayBaseUrl') as HTMLInputElement,
  computeMode: document.getElementById('computeMode') as HTMLSelectElement,
  prompt: document.getElementById('prompt') as HTMLTextAreaElement,
  status: document.getElementById('status') as HTMLDivElement,
  output: document.getElementById('output') as HTMLPreElement,
  saveConfig: document.getElementById('saveConfig') as HTMLButtonElement,
  start: document.getElementById('start') as HTMLButtonElement,
  cancel: document.getElementById('cancel') as HTMLButtonElement,
  forward: document.getElementById('forward') as HTMLButtonElement
};

let activeRunId = '';

const setStatus = (value: string) => {
  el.status.textContent = value;
};

const load = async () => {
  const info = await invoke<BackendInfo>('detect_backend');
  el.backend.textContent = `Preferred backend: ${info.preferredLabel} (${info.reason})`;

  const cfg = await invoke<AppConfig>('load_config');
  el.modelPath.value = cfg.modelPath;
  el.relayBaseUrl.value = cfg.relayBaseUrl;
  el.computeMode.value = cfg.computeMode;
};

el.saveConfig.onclick = async () => {
  await invoke('save_config', {
    config: {
      modelPath: el.modelPath.value,
      relayBaseUrl: el.relayBaseUrl.value,
      computeMode: el.computeMode.value
    }
  });
  setStatus('idle (config saved)');
};

el.start.onclick = async () => {
  el.output.textContent = '';
  setStatus('starting');
  activeRunId = await invoke<string>('start_inference', {
    modelPath: el.modelPath.value,
    prompt: el.prompt.value,
    computeMode: el.computeMode.value
  });
};

el.cancel.onclick = async () => {
  if (!activeRunId) return;
  await invoke('cancel_inference', { runId: activeRunId });
};

el.forward.onclick = async () => {
  const response = await invoke<{ status: string; relayResponse: string }>('encrypt_and_forward_output', {
    relayBaseUrl: el.relayBaseUrl.value,
    plaintextOutput: el.output.textContent ?? ''
  });
  setStatus(`forwarded: ${response.status}`);
};

await listen<InferenceEvent>('inference_event', (event) => {
  const payload = event.payload;
  if (payload.runId !== activeRunId) return;

  if (payload.type === 'started') setStatus('streaming');
  if (payload.type === 'token') el.output.textContent += payload.text ?? '';
  if (payload.type === 'done') setStatus('completed');
  if (payload.type === 'canceled') setStatus('canceled');
  if (payload.type === 'error') setStatus(`failed: ${payload.code ?? 'unknown'}`);
});

load().catch((error) => {
  setStatus(`failed to load: ${(error as Error).message}`);
});
