import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { open } from '@tauri-apps/plugin-dialog';
import { useEffect, useMemo, useRef, useState } from 'react';

type UiState = 'idle' | 'starting' | 'streaming' | 'canceled' | 'completed' | 'failed';
type BackendMode = 'auto' | 'cpu' | 'gpu' | 'hybrid';
type ContextTier = '8k-fast' | '64k-full';

interface BackendInfo {
  platform_label: string;
  preferred_mode: BackendMode;
  available_backend: 'cpu' | 'cuda' | 'metal';
  availability_label: string;
}

interface DesktopConfig {
  model_path: string;
  relay_base_url: string;
  relay_base_urls: string[];
  preferred_mode: BackendMode;
  selected_context_tier: ContextTier;
}

const DEFAULT_RELAY_BASE_URL = 'https://token.place';
const DEFAULT_CONTEXT_TIER: ContextTier = '8k-fast';
const CONTEXT_PROFILES: Array<{ id: ContextTier; label: string; contextWindowTokens: number }> = [
  { id: '8k-fast', label: '8K Fast', contextWindowTokens: 8192 },
  { id: '64k-full', label: '64K Full', contextWindowTokens: 65536 },
];
export const MAX_RELAY_BASE_URLS = 10;

type PartialDesktopConfig = Omit<Partial<DesktopConfig>, 'model_path' | 'relay_base_url' | 'relay_base_urls' | 'preferred_mode' | 'selected_context_tier'> & {
  model_path?: unknown;
  relay_base_url?: unknown;
  relay_base_urls?: unknown;
  preferred_mode?: unknown;
  selected_context_tier?: unknown;
};

interface RelayStatus {
  relay_url: string;
  registered: boolean;
  relay_runtime_state: string | null;
  last_error: string | null;
  last_request_id: string | null;
  request_count?: number;
}

interface ComputeNodeStatus {
  running: boolean;
  registered: boolean;
  active_relay_url: string;
  configured_relay_urls: string[];
  relay_statuses: RelayStatus[];
  registered_relay_count: number;
  configured_relay_count: number;
  registered_relay_urls: string[];
  active_relay_urls: string[];
  requested_mode: string | null;
  effective_mode: string | null;
  backend_available: string | null;
  backend_selected: string | null;
  backend_used: string | null;
  fallback_reason: string | null;
  model_path: string;
  last_error: string | null;
  relay_runtime_state: string | null;
  warm_load_state: string | null;
  warm_load_enabled: boolean | null;
  warm_load_duration_ms: number | null;
  runtime_path: string | null;
  relay_runtime_path: string | null;
  worker_state: string | null;
  worker_generation: number | null;
  worker_restart_count: number | null;
  worker_alive: boolean | null;
  last_worker_error_code: string | null;
  last_worker_exit_code: number | null;
  last_worker_restart_at_ms: number | null;
  operator_session_id: string | null;
  sequence: number | null;
  updated_at_ms: number | null;
  log_file_path: string | null;
  context_tier: string | null;
  context_window_tokens: number | null;
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

const defaultComputeStatus: ComputeNodeStatus = {
  running: false,
  registered: false,
  active_relay_url: '',
  configured_relay_urls: [],
  relay_statuses: [],
  registered_relay_count: 0,
  configured_relay_count: 0,
  registered_relay_urls: [],
  active_relay_urls: [],
  requested_mode: 'auto',
  effective_mode: null,
  backend_available: null,
  backend_selected: null,
  backend_used: null,
  fallback_reason: null,
  model_path: '',
  last_error: null,
  relay_runtime_state: 'idle',
  warm_load_state: null,
  warm_load_enabled: null,
  warm_load_duration_ms: null,
  runtime_path: null,
  relay_runtime_path: null,
  worker_state: null,
  worker_generation: null,
  worker_restart_count: null,
  worker_alive: null,
  last_worker_error_code: null,
  last_worker_exit_code: null,
  last_worker_restart_at_ms: null,
  operator_session_id: null,
  sequence: null,
  updated_at_ms: null,
  log_file_path: null,
  context_tier: DEFAULT_CONTEXT_TIER,
  context_window_tokens: 8192,
};

function formatErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function normalizeContextTier(value: unknown): ContextTier {
  return CONTEXT_PROFILES.some((profile) => profile.id === value) ? (value as ContextTier) : DEFAULT_CONTEXT_TIER;
}

function displayStatusValue(value: string | null | undefined, fallback: string): string {
  return value && value.trim() ? value : fallback;
}

function stringArrayPayload(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function relayStatusesPayload(value: unknown): RelayStatus[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    .map((item) => ({
      relay_url: typeof item.relay_url === 'string' ? item.relay_url : '',
      registered: item.registered === true,
      relay_runtime_state: typeof item.relay_runtime_state === 'string' ? item.relay_runtime_state : null,
      last_error: typeof item.last_error === 'string' ? item.last_error : null,
      last_request_id: typeof item.last_request_id === 'string' ? item.last_request_id : null,
      request_count: typeof item.request_count === 'number' ? item.request_count : undefined,
    }))
    .filter((item) => item.relay_url.trim());
}

function formatRegisteredLabel(status: ComputeNodeStatus, fallbackRelayCount: number): string {
  const configuredUrls = Array.isArray(status.configured_relay_urls) ? status.configured_relay_urls : [];
  const configuredCount = status.configured_relay_count || configuredUrls.length || fallbackRelayCount || 1;
  const runtimeState = status.relay_runtime_state === 'idle' ? status.warm_load_state : status.relay_runtime_state;
  const ready = status.running && (status.warm_load_enabled === false || runtimeState === 'ready' || runtimeState === 'processing');
  const registeredCount = ready ? status.registered_relay_count || (status.registered ? 1 : 0) : 0;
  if (registeredCount >= configuredCount && configuredCount > 0) {
    return `yes (${registeredCount}/${configuredCount} relays)`;
  }
  if (registeredCount > 0) {
    return `partial (${registeredCount}/${configuredCount} relays)`;
  }
  return `no (${registeredCount}/${configuredCount} relays)`;
}

export function normalizeRelayUrls(
  relayBaseUrls: unknown,
  legacyRelayBaseUrl = '',
  fallback = DEFAULT_RELAY_BASE_URL
): string[] {
  const rawUrls = Array.isArray(relayBaseUrls) ? relayBaseUrls : [];
  const normalized: string[] = [];

  for (const rawUrl of rawUrls) {
    if (typeof rawUrl !== 'string') {
      continue;
    }
    const trimmed = rawUrl.trim();
    if (!trimmed || normalized.includes(trimmed)) {
      continue;
    }
    normalized.push(trimmed);
    if (normalized.length >= MAX_RELAY_BASE_URLS) {
      break;
    }
  }

  if (normalized.length === 0) {
    const legacy = legacyRelayBaseUrl.trim();
    normalized.push(legacy || fallback);
  }

  return normalized;
}

function normalizeBackendMode(preferredMode: unknown): BackendMode {
  return preferredMode === 'cpu' ||
    preferredMode === 'gpu' ||
    preferredMode === 'hybrid' ||
    preferredMode === 'auto'
    ? preferredMode
    : 'auto';
}

export function normalizeDesktopConfig(config: PartialDesktopConfig): DesktopConfig {
  const legacyRelayBaseUrl = typeof config.relay_base_url === 'string' ? config.relay_base_url : '';
  const relayBaseUrls = normalizeRelayUrls(config.relay_base_urls, legacyRelayBaseUrl);
  return {
    model_path: typeof config.model_path === 'string' ? config.model_path : '',
    relay_base_url: relayBaseUrls[0] || DEFAULT_RELAY_BASE_URL,
    relay_base_urls: relayBaseUrls,
    preferred_mode: normalizeBackendMode(config.preferred_mode),
    selected_context_tier: normalizeContextTier(config.selected_context_tier),
  };
}

function configForSave(config: DesktopConfig): DesktopConfig {
  return normalizeDesktopConfig(config);
}

export function primaryRelayUrl(config: DesktopConfig): string {
  return normalizeRelayUrls(config.relay_base_urls, config.relay_base_url)[0] || DEFAULT_RELAY_BASE_URL;
}

export function updateRelayUrlAtIndex(
  config: DesktopConfig,
  index: number,
  value: string
): DesktopConfig {
  const relayBaseUrls = [...config.relay_base_urls];
  relayBaseUrls[index] = value;
  return {
    ...config,
    relay_base_url: primaryRelayUrl({ ...config, relay_base_urls: relayBaseUrls }),
    relay_base_urls: relayBaseUrls,
  };
}

export function addRelayUrl(config: DesktopConfig): DesktopConfig {
  if (config.relay_base_urls.length >= MAX_RELAY_BASE_URLS) {
    return config;
  }
  return { ...config, relay_base_urls: [...config.relay_base_urls, ''] };
}

export function removeRelayUrl(config: DesktopConfig, index: number): DesktopConfig {
  if (config.relay_base_urls.length <= 1) {
    return config;
  }
  const relayBaseUrls = config.relay_base_urls.filter((_, currentIndex) => currentIndex !== index);
  if (relayBaseUrls.length === 0) {
    relayBaseUrls.push(DEFAULT_RELAY_BASE_URL);
  }
  return {
    ...config,
    relay_base_url: primaryRelayUrl({ ...config, relay_base_urls: relayBaseUrls }),
    relay_base_urls: relayBaseUrls,
  };
}

function mergeComputeStatusEvent(
  prev: ComputeNodeStatus,
  payload: Record<string, unknown>
): ComputeNodeStatus {
  const payloadSession = typeof payload.operator_session_id === 'string' ? payload.operator_session_id : null;
  const payloadSequence = typeof payload.sequence === 'number' ? payload.sequence : null;
  const isFreshSessionEvent =
    (payload.type === 'started' || payload.type === 'error') &&
    payloadSequence === 1 &&
    !prev.running &&
    payloadSession !== null &&
    payloadSession !== prev.operator_session_id;
  if (
    prev.operator_session_id &&
    payloadSession &&
    payloadSession !== prev.operator_session_id &&
    !isFreshSessionEvent
  ) {
    return prev;
  }
  if (
    payloadSequence !== null &&
    prev.sequence !== null &&
    payloadSequence <= prev.sequence &&
    !isFreshSessionEvent
  ) {
    return prev;
  }
  const payloadWorkerGeneration =
    typeof payload.worker_generation === 'number' ? payload.worker_generation : null;
  if (
    payloadWorkerGeneration !== null &&
    prev.worker_generation !== null &&
    payloadWorkerGeneration < prev.worker_generation &&
    !isFreshSessionEvent
  ) {
    return prev;
  }

  return {
    running:
      typeof payload.running === 'boolean'
        ? payload.running
        : payload.type === 'error'
          ? false
          : prev.running,
    registered:
      typeof payload.registered === 'boolean'
        ? payload.registered
        : payload.type === 'error'
          ? false
          : prev.registered,
    active_relay_url:
      typeof payload.active_relay_url === 'string'
        ? payload.active_relay_url
        : prev.active_relay_url,
    configured_relay_urls:
      Array.isArray(payload.configured_relay_urls)
        ? stringArrayPayload(payload.configured_relay_urls)
        : prev.configured_relay_urls,
    relay_statuses:
      Array.isArray(payload.relay_statuses)
        ? relayStatusesPayload(payload.relay_statuses)
        : prev.relay_statuses,
    registered_relay_count:
      typeof payload.registered_relay_count === 'number'
        ? payload.registered_relay_count
        : payload.type === 'error'
          ? 0
          : prev.registered_relay_count,
    configured_relay_count:
      typeof payload.configured_relay_count === 'number'
        ? payload.configured_relay_count
        : prev.configured_relay_count,
    registered_relay_urls:
      Array.isArray(payload.registered_relay_urls)
        ? stringArrayPayload(payload.registered_relay_urls)
        : payload.type === 'error'
          ? []
          : prev.registered_relay_urls,
    active_relay_urls:
      Array.isArray(payload.active_relay_urls)
        ? stringArrayPayload(payload.active_relay_urls)
        : payload.type === 'error'
          ? []
          : prev.active_relay_urls,
    requested_mode:
      typeof payload.requested_mode === 'string' ? payload.requested_mode : prev.requested_mode,
    effective_mode:
      typeof payload.effective_mode === 'string' ? payload.effective_mode : prev.effective_mode,
    backend_available:
      typeof payload.backend_available === 'string'
        ? payload.backend_available
        : prev.backend_available,
    backend_selected:
      typeof payload.backend_selected === 'string'
        ? payload.backend_selected
        : prev.backend_selected,
    backend_used:
      typeof payload.backend_used === 'string' ? payload.backend_used : prev.backend_used,
    fallback_reason:
      payload.fallback_reason === null
        ? null
        : typeof payload.fallback_reason === 'string'
          ? payload.fallback_reason
          : prev.fallback_reason,
    model_path: typeof payload.model_path === 'string' ? payload.model_path : prev.model_path,
    relay_runtime_state:
      typeof payload.relay_runtime_state === 'string'
        ? payload.relay_runtime_state
        : typeof payload.warm_load_state === 'string'
          ? payload.warm_load_state
          : payload.type === 'error'
            ? 'failed'
            : prev.relay_runtime_state,
    warm_load_state:
      typeof payload.warm_load_state === 'string'
        ? payload.warm_load_state
        : payload.type === 'error'
          ? 'failed'
          : prev.warm_load_state,
    warm_load_enabled:
      typeof payload.warm_load_enabled === 'boolean'
        ? payload.warm_load_enabled
        : prev.warm_load_enabled,
    warm_load_duration_ms:
      typeof payload.warm_load_duration_ms === 'number'
        ? payload.warm_load_duration_ms
        : prev.warm_load_duration_ms,
    runtime_path: typeof payload.runtime_path === 'string' ? payload.runtime_path : prev.runtime_path,
    relay_runtime_path:
      typeof payload.relay_runtime_path === 'string'
        ? payload.relay_runtime_path
        : prev.relay_runtime_path,
    worker_state:
      typeof payload.worker_state === 'string'
        ? payload.worker_state
        : payload.type === 'error'
          ? 'failed'
          : prev.worker_state,
    worker_generation:
      typeof payload.worker_generation === 'number' ? payload.worker_generation : prev.worker_generation,
    worker_restart_count:
      typeof payload.worker_restart_count === 'number' ? payload.worker_restart_count : prev.worker_restart_count,
    worker_alive:
      typeof payload.worker_alive === 'boolean'
        ? payload.worker_alive
        : payload.type === 'error'
          ? false
          : prev.worker_alive,
    last_worker_error_code:
      payload.last_worker_error_code === null
        ? null
        : typeof payload.last_worker_error_code === 'string'
          ? payload.last_worker_error_code
          : prev.last_worker_error_code,
    last_worker_exit_code:
      payload.last_worker_exit_code === null
        ? null
        : typeof payload.last_worker_exit_code === 'number'
          ? payload.last_worker_exit_code
          : prev.last_worker_exit_code,
    last_worker_restart_at_ms:
      payload.last_worker_restart_at_ms === null
        ? null
        : typeof payload.last_worker_restart_at_ms === 'number'
          ? payload.last_worker_restart_at_ms
          : prev.last_worker_restart_at_ms,
    operator_session_id: payloadSession ?? prev.operator_session_id,
    sequence: payloadSequence ?? prev.sequence,
    updated_at_ms:
      typeof payload.updated_at_ms === 'number' ? payload.updated_at_ms : prev.updated_at_ms,
    log_file_path:
      payload.log_file_path === null
        ? null
        : typeof payload.log_file_path === 'string'
          ? payload.log_file_path
          : prev.log_file_path,
    last_error:
      payload.last_error === null
        ? null
        : typeof payload.last_error === 'string'
          ? payload.last_error
          : typeof payload.message === 'string'
            ? payload.message
            : prev.last_error,
    context_tier:
      typeof payload.context_tier === 'string' ? payload.context_tier : prev.context_tier,
    context_window_tokens:
      typeof payload.context_window_tokens === 'number'
        ? payload.context_window_tokens
        : prev.context_window_tokens,
  };
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
    relay_base_url: DEFAULT_RELAY_BASE_URL,
    relay_base_urls: [DEFAULT_RELAY_BASE_URL],
    preferred_mode: 'auto',
    selected_context_tier: DEFAULT_CONTEXT_TIER,
  });
  const [computeStatus, setComputeStatus] = useState<ComputeNodeStatus>(defaultComputeStatus);
  const [prompt, setPrompt] = useState('');
  const [output, setOutput] = useState('');
  const [requestId, setRequestId] = useState<string>('');
  const [status, setStatus] = useState<UiState>('idle');
  const [artifact, setArtifact] = useState<ModelArtifactInfo | null>(null);
  const [isDownloadingModel, setIsDownloadingModel] = useState(false);
  const [error, setError] = useState('');
  const [isForwarding, setIsForwarding] = useState(false);
  const [isStartingComputeNode, setIsStartingComputeNode] = useState(false);
  const [operatorLogText, setOperatorLogText] = useState('');
  const [isDebugConsoleOpen, setIsDebugConsoleOpen] = useState(false);
  const relayRuntimeState =
    computeStatus.relay_runtime_state && computeStatus.relay_runtime_state !== 'idle'
      ? computeStatus.relay_runtime_state
      : computeStatus.warm_load_state || 'idle';
  const relayRuntimeReady =
    computeStatus.warm_load_enabled === false ||
    relayRuntimeState === 'ready' ||
    relayRuntimeState === 'processing';
  const computeNodeRegistered = computeStatus.running && computeStatus.registered && relayRuntimeReady;
  const saveTimerRef = useRef<number | null>(null);
  const requestIdRef = useRef('');
  const computeStatusRef = useRef<ComputeNodeStatus>(defaultComputeStatus);

  useEffect(() => {
    invoke<BackendInfo>('detect_backend')
      .then(setBackend)
      .catch((e) => setError(formatErrorMessage(e)));

    const initializeConfigAndArtifact = async () => {
      try {
        const loadedConfig = await invoke<PartialDesktopConfig>('load_config');
        const normalizedConfig = normalizeDesktopConfig(loadedConfig);
        setConfig(normalizedConfig);
        if (JSON.stringify(loadedConfig) !== JSON.stringify(normalizedConfig)) {
          invoke('save_config', { config: normalizedConfig }).catch((e) => setError(formatErrorMessage(e)));
        }
        const nodeStatus = { ...defaultComputeStatus, ...(await invoke<Partial<ComputeNodeStatus>>('get_compute_node_status')) };
        computeStatusRef.current = nodeStatus;
        setComputeStatus(nodeStatus);

        const info = await invoke<ModelArtifactInfo>('inspect_model_artifact');
        setArtifact(info);

      } catch (e) {
        setError(formatErrorMessage(e));
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
    const unlisten = listen<Record<string, unknown>>('compute_node_event', (evt) => {
      const payload = evt.payload;
      const previous = computeStatusRef.current;
      const next = mergeComputeStatusEvent(previous, payload);
      if (next === previous) {
        return;
      }
      computeStatusRef.current = next;
      setComputeStatus(next);
      if (payload.type === 'started' || payload.type === 'error') {
        setIsStartingComputeNode(false);
      }
      if (payload.type === 'error') {
        const computeMessage =
          typeof payload.last_error === 'string'
            ? payload.last_error
            : typeof payload.message === 'string'
              ? payload.message
              : 'compute-node operator failed';
        setError(computeMessage);
      }
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
    () =>
      Boolean(config.model_path.trim()) &&
      Boolean(prompt.trim()) &&
      status !== 'starting' &&
      status !== 'streaming',
    [config.model_path, prompt, status]
  );

  const canStartComputeNode = useMemo(
    () => Boolean(config.model_path.trim()) && !computeStatus.running && !isStartingComputeNode,
    [config.model_path, computeStatus.running, isStartingComputeNode]
  );
  const availableBackend = backend?.available_backend ?? 'cpu';
  const gpuCapable = availableBackend === 'metal' || availableBackend === 'cuda';

  const scheduleConfigSave = (next: DesktopConfig) => {
    if (saveTimerRef.current !== null) {
      window.clearTimeout(saveTimerRef.current);
    }
    saveTimerRef.current = window.setTimeout(() => {
      invoke('save_config', { config: configForSave(next) }).catch((e) => setError(formatErrorMessage(e)));
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
      setError(formatErrorMessage(e));
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
      setError(formatErrorMessage(e));
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
    try {
      await invoke('start_inference', {
        request: {
          request_id: nextRequestId,
          model_path: config.model_path,
          prompt,
          mode: config.preferred_mode,
        },
      });
    } catch (e) {
      setStatus('failed');
      setError(formatErrorMessage(e));
    }
  };

  const cancelInference = async () => {
    if (!requestIdRef.current) return;
    await invoke('cancel_inference', { request_id: requestIdRef.current });
  };

  const startComputeNode = async () => {
    try {
      setIsStartingComputeNode(true);
      setError('');
      const optimisticStatus = {
        ...computeStatusRef.current,
        running: false,
        registered: false,
        relay_runtime_state: 'starting',
        sequence: computeStatusRef.current.operator_session_id ? Number.MAX_SAFE_INTEGER : null,
        active_relay_url: primaryRelayUrl(config),
        configured_relay_urls: normalizeRelayUrls(config.relay_base_urls, config.relay_base_url),
        relay_statuses: normalizeRelayUrls(config.relay_base_urls, config.relay_base_url).map((relayUrl) => ({
          relay_url: relayUrl,
          registered: false,
          relay_runtime_state: 'starting',
          last_error: null,
          last_request_id: null,
        })),
        registered_relay_count: 0,
        configured_relay_count: normalizeRelayUrls(config.relay_base_urls, config.relay_base_url).length,
        registered_relay_urls: [],
        active_relay_urls: [],
        requested_mode: config.preferred_mode,
        effective_mode: null,
        backend_available: null,
        backend_selected: null,
        backend_used: null,
        fallback_reason: null,
        model_path: config.model_path,
        last_error: null,
        worker_state: 'starting',
        worker_alive: false,
        log_file_path: null,
        context_tier: config.selected_context_tier,
        context_window_tokens: CONTEXT_PROFILES.find((profile) => profile.id === config.selected_context_tier)?.contextWindowTokens ?? 8192,
      };
      computeStatusRef.current = optimisticStatus;
      setComputeStatus(optimisticStatus);
      await invoke('start_compute_node', {
        request: {
          model_path: config.model_path,
          relay_base_url: primaryRelayUrl(config),
          relay_base_urls: normalizeRelayUrls(config.relay_base_urls, config.relay_base_url),
          mode: config.preferred_mode,
          context_tier: config.selected_context_tier,
        },
      });
    } catch (e) {
      setIsStartingComputeNode(false);
      const message = formatErrorMessage(e);
      const failedStatus = {
        ...computeStatusRef.current,
        running: false,
        registered: false,
        relay_runtime_state: 'failed',
        worker_state: 'failed',
        worker_alive: false,
        last_error: message,
      };
      computeStatusRef.current = failedStatus;
      setComputeStatus(failedStatus);
      setError(message);
    }
  };

  const stopComputeNode = async () => {
    try {
      await invoke('stop_compute_node');
    } catch (e) {
      setError(formatErrorMessage(e));
    }
  };


  const refreshOperatorLog = async () => {
    try {
      const logText = await invoke<string>('read_operator_log');
      setOperatorLogText(logText);
      setIsDebugConsoleOpen(true);
    } catch (e) {
      setError(formatErrorMessage(e));
    }
  };

  const revealOperatorLog = async () => {
    try {
      await invoke('reveal_operator_log');
    } catch (e) {
      setError(formatErrorMessage(e));
    }
  };

  const copyOperatorLogPath = async () => {
    try {
      const logPath = computeStatus.log_file_path;
      if (!logPath) return;
      const writeText = navigator.clipboard?.writeText;
      if (!writeText) {
        setError('Clipboard API is unavailable in this webview.');
        return;
      }
      await writeText.call(navigator.clipboard, logPath);
    } catch (e) {
      setError(formatErrorMessage(e));
    }
  };

  const openOperatorDebugTerminal = async () => {
    try {
      await invoke('open_operator_debug_terminal');
    } catch (e) {
      setError(formatErrorMessage(e));
    }
  };

  const forwardEncrypted = async () => {
    try {
      setIsForwarding(true);
      setError('');
      await invoke('encrypt_and_forward', {
        relay_base_url: primaryRelayUrl(config),
        final_output: output,
      });
    } catch (e) {
      setError(formatErrorMessage(e));
    } finally {
      setIsForwarding(false);
    }
  };

  return (
    <main style={{ maxWidth: 820, margin: '20px auto', fontFamily: 'sans-serif' }}>
      <h1>token.place desktop compute node</h1>
      <p>Platform GPU availability: <strong>{backend?.availability_label ?? 'loading...'}</strong></p>
      <label htmlFor="model-path-input">Model GGUF path</label>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          id="model-path-input"
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
      <select
        value={config.preferred_mode}
        onChange={(e) => updateConfig({ ...config, preferred_mode: e.target.value as BackendMode })}
      >
        <option value="auto">Auto</option>
        <option value="cpu">CPU only</option>
        <option value="gpu" disabled={!gpuCapable}>GPU only</option>
        <option value="hybrid" disabled={!gpuCapable}>Hybrid (partial GPU offload)</option>
      </select>
      <p style={{ marginTop: 8, fontSize: 12, color: '#555' }}>
        Operator note: GPU mode requests full offload when CUDA/Metal is available; Hybrid requests
        partial offload. Unsupported platforms fall back to CPU with diagnostics.
      </p>

      <label htmlFor="context-tier-select" style={{ display: 'block', marginTop: 12 }}>Context tier</label>
      <select
        id="context-tier-select"
        value={config.selected_context_tier}
        disabled={computeStatus.running || isStartingComputeNode}
        onChange={(e) => updateConfig({ ...config, selected_context_tier: normalizeContextTier(e.target.value) })}
      >
        {CONTEXT_PROFILES.map((profile) => (
          <option key={profile.id} value={profile.id}>{profile.label}</option>
        ))}
      </select>
      <p style={{ marginTop: 8, fontSize: 12, color: '#555' }}>
        Changing tiers requires Stop Operator followed by Start Operator. The active operator warms one selected tier per process.
      </p>

      <section aria-labelledby="relay-urls-heading" style={{ marginTop: 12 }}>
        <h2 id="relay-urls-heading" style={{ fontSize: 16, marginBottom: 8 }}>Relay URLs</h2>
        {(computeStatus.running || isStartingComputeNode) && (
          <p style={{ marginTop: 0, fontSize: 12, color: '#555' }}>
            Stop the operator to edit relay URLs. Changes apply on next start.
          </p>
        )}
        {config.relay_base_urls.map((relayUrl, index) => {
          const inputId = `relay-url-${index}`;
          const relayControlsDisabled = computeStatus.running || isStartingComputeNode;
          return (
            <div key={index} style={{ display: 'flex', gap: 8, marginTop: 6, alignItems: 'center' }}>
              <label htmlFor={inputId} style={{ minWidth: 92 }}>Relay URL {index + 1}</label>
              <input
                id={inputId}
                value={relayUrl}
                disabled={relayControlsDisabled}
                style={{ width: '100%' }}
                onChange={(e) => updateConfig(updateRelayUrlAtIndex(config, index, e.target.value))}
              />
              {index > 0 && (
                <button
                  type="button"
                  disabled={relayControlsDisabled}
                  onClick={() => updateConfig(removeRelayUrl(config, index))}
                  aria-label={`Delete relay URL ${index + 1}`}
                >
                  Delete
                </button>
              )}
            </div>
          );
        })}
        <button
          type="button"
          onClick={() => updateConfig(addRelayUrl(config))}
          disabled={computeStatus.running || isStartingComputeNode || config.relay_base_urls.length >= MAX_RELAY_BASE_URLS}
          style={{ marginTop: 8 }}
        >
          Add new relay URL
        </button>
        <p style={{ marginTop: 6, fontSize: 12, color: '#555' }}>
          Up to {MAX_RELAY_BASE_URLS} relay URLs are supported. Blank entries are ignored when saved or started.
        </p>
      </section>

      <section style={{ marginTop: 14, border: '1px solid #ddd', padding: 12 }}>
        <h2 style={{ marginTop: 0 }}>Compute node operator</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          <button disabled={!canStartComputeNode} onClick={startComputeNode}>Start operator</button>
          <button
            disabled={!computeStatus.running}
            onClick={stopComputeNode}
          >
            Stop operator
          </button>
        </div>
        <p style={{ marginBottom: 0 }}>Running: <strong>{computeStatus.running ? 'yes' : 'no'}</strong></p>
        <p style={{ marginBottom: 0 }}>Registered: <strong>{formatRegisteredLabel(computeStatus, normalizeRelayUrls(config.relay_base_urls, config.relay_base_url).length)}</strong></p>
        <p style={{ marginBottom: 0 }}>Relay runtime state: <code>{relayRuntimeState}</code></p>
        <p style={{ marginBottom: 0 }}>Runtime path: <code>{displayStatusValue(computeStatus.runtime_path, 'pending')}</code></p>
        <p style={{ marginBottom: 0 }}>Relay runtime path: <code>{displayStatusValue(computeStatus.relay_runtime_path, 'pending')}</code></p>
        <p style={{ marginBottom: 0 }}>Worker state: <strong>{displayStatusValue(computeStatus.worker_state, computeStatus.running ? 'starting' : 'stopped')}</strong></p>
        <p style={{ marginBottom: 0 }}>Worker alive: <strong>{computeStatus.worker_alive === null ? 'unknown' : computeStatus.worker_alive ? 'yes' : 'no'}</strong></p>
        <p style={{ marginBottom: 0 }}>Worker generation: <code>{computeStatus.worker_generation ?? 'unknown'}</code></p>
        <p style={{ marginBottom: 0 }}>Worker restart count: <code>{computeStatus.worker_restart_count ?? 0}</code></p>
        <p style={{ marginBottom: 0 }}>Last worker error code: <code>{computeStatus.last_worker_error_code || 'none'}</code></p>
        <p style={{ marginBottom: 0 }}>Last worker exit code: <code>{computeStatus.last_worker_exit_code ?? 'none'}</code></p>
        <p style={{ marginBottom: 0 }}>Active relay URL: <code>{displayStatusValue(computeStatus.active_relay_url, primaryRelayUrl(config))}</code></p>
        <p style={{ marginBottom: 0 }}>Configured relay URLs: <code>{((Array.isArray(computeStatus.configured_relay_urls) && computeStatus.configured_relay_urls.length) ? computeStatus.configured_relay_urls : normalizeRelayUrls(config.relay_base_urls, config.relay_base_url)).join(', ')}</code></p>
        {Array.isArray(computeStatus.relay_statuses) && computeStatus.relay_statuses.length > 0 && (
          <div style={{ marginTop: 8 }}>
            <strong>Per-relay status</strong>
            <ul style={{ marginTop: 4, paddingLeft: 20 }}>
              {computeStatus.relay_statuses.map((relayStatus) => (
                <li key={relayStatus.relay_url}>
                  <code>{relayStatus.relay_url}</code> — {relayStatus.registered ? 'registered' : 'not registered'}
                  {relayStatus.relay_runtime_state ? ` (${relayStatus.relay_runtime_state})` : ''}
                  {relayStatus.last_request_id ? ` request ${relayStatus.last_request_id}` : ''}
                  {relayStatus.last_error ? ` — ${relayStatus.last_error}` : ''}
                </li>
              ))}
            </ul>
          </div>
        )}
        <p style={{ marginBottom: 0 }}>Context tier: <code>{displayStatusValue(computeStatus.context_tier, config.selected_context_tier)}</code></p>
        <p style={{ marginBottom: 0 }}>Context window: <code>{computeStatus.context_window_tokens ?? (CONTEXT_PROFILES.find((profile) => profile.id === config.selected_context_tier)?.contextWindowTokens ?? 8192)} tokens</code></p>
        <p style={{ marginBottom: 0 }}>Requested mode: <code>{displayStatusValue(computeStatus.requested_mode, config.preferred_mode)}</code></p>
        <p style={{ marginBottom: 0 }}>Effective mode: <code>{displayStatusValue(computeStatus.effective_mode, 'pending')}</code></p>
        <p style={{ marginBottom: 0 }}>Backend available: <code>{displayStatusValue(computeStatus.backend_available, 'pending')}</code></p>
        <p style={{ marginBottom: 0 }}>Backend selected: <code>{displayStatusValue(computeStatus.backend_selected, 'pending')}</code></p>
        <p style={{ marginBottom: 0 }}>Backend used: <code>{displayStatusValue(computeStatus.backend_used, 'pending')}</code></p>
        <p style={{ marginBottom: 0 }}>Fallback reason: <code>{computeStatus.fallback_reason || 'none'}</code></p>
        <p style={{ marginBottom: 0 }}>Model path: <code>{computeStatus.model_path || config.model_path || 'not set'}</code></p>
        <p style={{ marginBottom: 0 }}>Operator debug log: <code>{computeStatus.log_file_path || 'not created yet'}</code></p>
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
          <button type="button" disabled={!computeStatus.log_file_path} onClick={refreshOperatorLog}>
            Open debug log
          </button>
          <button type="button" disabled={!computeStatus.log_file_path} onClick={revealOperatorLog}>
            Reveal log file
          </button>
          <button type="button" disabled={!computeStatus.log_file_path} onClick={copyOperatorLogPath}>
            Copy log path
          </button>
          <button type="button" disabled={!computeStatus.log_file_path} onClick={openOperatorDebugTerminal}>
            Open debug terminal
          </button>
        </div>
        {isDebugConsoleOpen && (
          <section aria-label="Operator debug console" style={{ marginTop: 10 }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <strong>Operator debug console</strong>
              <button type="button" onClick={refreshOperatorLog}>Refresh</button>
              <button
                type="button"
                onClick={() => {
                  const writeText = navigator.clipboard?.writeText;
                  if (!writeText) {
                    setError('Clipboard API is unavailable in this webview.');
                    return;
                  }
                  writeText.call(navigator.clipboard, operatorLogText).catch((err) =>
                    setError(formatErrorMessage(err))
                  );
                }}
              >
                Copy log
              </button>
              <button type="button" onClick={() => setIsDebugConsoleOpen(false)}>Close</button>
            </div>
            <textarea
              readOnly
              rows={12}
              value={operatorLogText}
              style={{ width: '100%', marginTop: 8, fontFamily: 'monospace' }}
            />
          </section>
        )}
        <p style={{ marginBottom: 0 }}>Last error: <code>{computeStatus.last_error || 'none'}</code></p>
      </section>

      <section style={{ marginTop: 14, borderTop: '1px solid #ddd', paddingTop: 12 }}>
        <h2 style={{ marginTop: 0 }}>Local prompt smoke test</h2>
        <label style={{ display: 'block', marginTop: 12 }}>Prompt</label>
        <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={6} style={{ width: '100%' }} />

        <div style={{ marginTop: 10, display: 'flex', gap: 8 }}>
          <button disabled={!canStart} onClick={startInference}>Start local inference</button>
          <button disabled={status !== 'starting' && status !== 'streaming'} onClick={cancelInference}>Cancel</button>
          <button disabled={!output || isForwarding} onClick={forwardEncrypted}>
            Debug relay forward (disabled; API v1 E2EE only)
          </button>
        </div>

        <p>Status: <strong>{status}</strong></p>
      </section>

      {error && <p style={{ color: 'crimson' }}>Error: {error}</p>}
      <pre style={{ whiteSpace: 'pre-wrap', padding: 12, border: '1px solid #ddd' }}>{output}</pre>
    </main>
  );
}
