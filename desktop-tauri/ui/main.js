const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const { open } = window.__TAURI__.dialog;

const modelPath = document.getElementById('modelPath');
const computeMode = document.getElementById('computeMode');
const backend = document.getElementById('backend');
const prompt = document.getElementById('prompt');
const stateEl = document.getElementById('state');
const output = document.getElementById('output');
const relayUrl = document.getElementById('relayUrl');
const serverKey = document.getElementById('serverKey');

let finalOutput = '';

function setState(next) {
  stateEl.textContent = next;
}

async function refreshBackend() {
  const mode = computeMode.value;
  const detected = await invoke('detect_preferred_backend', { mode });
  backend.textContent = detected;
}

async function loadConfig() {
  const cfg = await invoke('get_config');
  modelPath.value = cfg.model_path || '';
  relayUrl.value = cfg.relay_base_url;
  computeMode.value = cfg.compute_mode;
  await refreshBackend();
}

document.getElementById('pickModel').addEventListener('click', async () => {
  const selected = await open({ multiple: false, filters: [{ name: 'GGUF', extensions: ['gguf'] }] });
  if (typeof selected === 'string') modelPath.value = selected;
});

computeMode.addEventListener('change', refreshBackend);

document.getElementById('startBtn').addEventListener('click', async () => {
  setState('starting');
  output.textContent = '';
  finalOutput = '';

  const cfg = await invoke('get_config');
  cfg.model_path = modelPath.value;
  cfg.relay_base_url = relayUrl.value;
  cfg.compute_mode = computeMode.value;
  await invoke('update_config', { config: cfg });

  await invoke('start_inference', {
    req: {
      model_path: modelPath.value,
      prompt: prompt.value,
      backend: backend.textContent,
    },
    config: cfg,
  });
});

document.getElementById('cancelBtn').addEventListener('click', async () => {
  await invoke('cancel_inference');
  setState('canceled');
});

document.getElementById('forwardBtn').addEventListener('click', async () => {
  await invoke('encrypt_and_forward', {
    request: {
      relay_base_url: relayUrl.value,
      server_public_key: serverKey.value,
      final_output: finalOutput,
    },
  });
});

listen('inference_event', (event) => {
  const data = event.payload;
  if (data.type === 'started') setState('streaming');
  if (data.type === 'token') {
    finalOutput += data.text;
    output.textContent = finalOutput;
  }
  if (data.type === 'done') setState('completed');
  if (data.type === 'canceled') setState('canceled');
  if (data.type === 'error') setState('failed');
});

listen('sidecar_log', () => {});

loadConfig();
