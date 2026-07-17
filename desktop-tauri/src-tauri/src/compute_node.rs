use crate::backend::ComputeMode;
use crate::config::normalize_relay_base_urls;
use crate::context_profiles::{context_profile, normalize_context_tier, DEFAULT_CONTEXT_TIER};
use crate::operator_logs::{
    append_line_to_path, read_log_tail, sanitize_operator_diagnostic_line,
    sanitize_operator_path_display, OperatorLogSink,
};
use crate::python_runtime::{
    bridge_script_candidates_from_resource_roots, configure_python_subprocess_env,
    describe_resource_layout, disable_python_user_site, resolve_bridge_script_path,
    resolve_python_launcher_resource_aware, resolve_runtime_import_root,
    should_enable_runtime_bootstrap, PythonLauncher, PythonLauncherResolutionOptions,
    ENABLE_RUNTIME_BOOTSTRAP_ENV,
};
use crate::subprocess_logging::{SubprocessLogFilter, SubprocessLogPolicy};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::collections::BTreeMap;
#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComputeNodeRequest {
    pub model_path: String,
    pub relay_base_url: String,
    #[serde(default)]
    pub relay_base_urls: Vec<String>,
    pub mode: ComputeMode,
    #[serde(default = "default_request_context_tier")]
    pub context_tier: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ComputeNodeStatus {
    pub running: bool,
    pub registered: bool,
    pub active_relay_url: String,
    #[serde(default)]
    pub configured_relay_urls: Vec<String>,
    #[serde(default)]
    pub relay_statuses: Vec<Value>,
    #[serde(default)]
    pub registered_relay_count: usize,
    #[serde(default)]
    pub configured_relay_count: usize,
    #[serde(default)]
    pub registered_relay_urls: Vec<String>,
    #[serde(default)]
    pub active_relay_urls: Vec<String>,
    pub requested_mode: String,
    pub effective_mode: String,
    pub backend_available: String,
    pub backend_selected: String,
    pub backend_used: String,
    pub fallback_reason: Option<String>,
    pub model_path: String,
    pub last_error: Option<String>,
    pub relay_runtime_state: Option<String>,
    pub warm_load_state: Option<String>,
    pub warm_load_enabled: Option<bool>,
    pub warm_load_duration_ms: Option<u64>,
    pub context_tier: Option<String>,
    pub context_window_tokens: Option<u32>,
    pub runtime_path: Option<String>,
    pub relay_runtime_path: Option<String>,
    pub worker_state: Option<String>,
    pub worker_generation: Option<u64>,
    pub worker_restart_count: Option<u64>,
    pub worker_alive: Option<bool>,
    pub last_worker_error_code: Option<String>,
    pub last_worker_exit_code: Option<i64>,
    pub last_worker_restart_at_ms: Option<u64>,
    pub operator_session_id: Option<String>,
    pub sequence: Option<u64>,
    pub updated_at_ms: Option<u64>,
    pub log_file_path: Option<String>,
    #[serde(default)]
    pub readiness_diagnostics: Map<String, Value>,
}

fn default_request_context_tier() -> String {
    DEFAULT_CONTEXT_TIER.to_string()
}

#[derive(Clone, Default)]
pub struct ComputeNodeState {
    pub child: Arc<Mutex<Option<Child>>>,
    pub stdin: Arc<Mutex<Option<ChildStdin>>>,
    pub status: Arc<Mutex<ComputeNodeStatus>>,
    pub lifecycle_lock: Arc<Mutex<()>>,
    pub next_session_id: Arc<Mutex<u64>>,
}

fn parse_compute_node_event_line(line: &str) -> Result<Value, serde_json::Error> {
    serde_json::from_str::<Value>(line)
}

fn build_bridge_command(
    bridge_path: &str,
    launcher: Option<PythonLauncher>,
) -> anyhow::Result<Command> {
    if is_python_script(bridge_path) {
        let launcher = launcher.ok_or_else(|| {
            anyhow::anyhow!("missing resolved Python launcher for compute-node bridge script")
        })?;
        return Ok(launcher.command_for_script(bridge_path));
    }

    Ok(Command::new(bridge_path))
}

fn is_python_script(path: &str) -> bool {
    Path::new(path)
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("py"))
}

fn bridge_script_candidates(
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    resource_dir: Option<&Path>,
) -> Vec<std::path::PathBuf> {
    bridge_script_candidates_from_resource_roots(
        "compute_node_bridge.py",
        exe_path,
        manifest_dir,
        resource_dir,
    )
}

fn resolve_bridge_script_for(
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    resource_dir: Option<&Path>,
    interpreter: Option<&str>,
) -> Result<String, String> {
    resolve_bridge_script_path(
        "compute_node_bridge.py",
        exe_path,
        manifest_dir,
        resource_dir,
        interpreter,
    )
    .map(|path| path.to_string_lossy().into_owned())
}

fn resolve_bridge_script(app: &AppHandle) -> Result<String, String> {
    let exe_path = std::env::current_exe().ok();
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let resource_dir = app.path().resource_dir().ok();
    resolve_bridge_script_for(
        exe_path.as_deref(),
        manifest_dir,
        resource_dir.as_deref(),
        None,
    )
}

fn first_existing_script(candidates: Vec<std::path::PathBuf>) -> Option<String> {
    candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .map(|candidate| candidate.to_string_lossy().into_owned())
}

fn configure_runtime_pythonpath(
    command: &mut Command,
    manifest_dir: &Path,
    bridge_script: &str,
) -> Option<std::path::PathBuf> {
    disable_python_user_site(command);
    let import_root = resolve_runtime_import_root(Some(Path::new(bridge_script)), manifest_dir);
    if let Some(import_root) = import_root.as_deref() {
        configure_python_subprocess_env(command, import_root);
    }
    import_root
}

fn configure_runtime_bootstrap_env(command: &mut Command, mode: &ComputeMode) {
    if should_enable_runtime_bootstrap(mode) {
        command.env(ENABLE_RUNTIME_BOOTSTRAP_ENV, "1");
    }
}

#[cfg(test)]
fn command_env_value(command: &Command, key: &str) -> Option<String> {
    command
        .as_std()
        .get_envs()
        .find_map(|(env_key, value)| (env_key == key).then_some(value))
        .flatten()
        .map(|value| value.to_string_lossy().into_owned())
}

fn sanitize_relay_target(relay_url: &str) -> String {
    let trimmed = relay_url.trim();
    let without_fragment = trimmed.split('#').next().unwrap_or(trimmed);
    let without_query = without_fragment
        .split('?')
        .next()
        .unwrap_or(without_fragment);
    if let Some((scheme, rest)) = without_query.split_once("://") {
        let safe_scheme = scheme
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '+' | '-' | '.'));
        if !safe_scheme || scheme.is_empty() {
            return "unknown".into();
        }
        let authority = rest
            .split(|ch: char| ch == '/' || ch.is_control() || ch.is_whitespace())
            .next()
            .unwrap_or(rest);
        let safe_authority = authority.rsplit('@').next().unwrap_or(authority);
        let safe_authority: String = safe_authority
            .chars()
            .filter(|ch| !ch.is_control() && !ch.is_whitespace())
            .collect();
        if !safe_authority.is_empty() {
            return format!("{scheme}://{safe_authority}");
        }
    }
    "unknown".into()
}

fn normalized_request_relay_urls(request: &ComputeNodeRequest) -> Vec<String> {
    normalize_relay_base_urls(&request.relay_base_urls, &request.relay_base_url)
}

fn current_time_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or_default()
}

fn event_session_id(payload: &Value) -> Option<&str> {
    payload.get("operator_session_id").and_then(Value::as_str)
}

const SAFE_READINESS_DIAGNOSTIC_KEYS: &[&str] = &[
    "api_v1_readiness_result",
    "api_v1_readiness_error_code",
    "api_v1_readiness_error_reason",
    "api_v1_readiness_yarn_requested_context_tokens",
    "api_v1_readiness_yarn_original_context_tokens",
    "api_v1_readiness_yarn_context_multiplier",
    "api_v1_readiness_yarn_rope_freq_scale",
    "api_v1_readiness_yarn_ext_factor_overridden",
    "api_v1_readiness_yarn_rope_scaling_type_source",
    "api_v1_readiness_yarn_configuration_valid",
    "api_v1_readiness_completion_smoke_result",
    "api_v1_readiness_completion_smoke_failure_reason",
    "api_v1_readiness_completion_smoke_error_code",
    "api_v1_readiness_completion_smoke_safe_summary",
    "api_v1_readiness_completion_smoke_exception_category",
    "api_v1_readiness_completion_smoke_exception_type",
    "api_v1_readiness_completion_smoke_rejected_generation_kwarg",
    "api_v1_readiness_completion_smoke_rejected_option",
    "api_v1_readiness_completion_smoke_attempted_generation_kwargs",
    "api_v1_readiness_completion_smoke_attempted_plain_completion_methods",
    "api_v1_readiness_completion_smoke_method",
    "api_v1_readiness_completion_smoke_generation_exception_category",
    "api_v1_readiness_completion_smoke_result_shape",
    "api_v1_readiness_completion_smoke_plain_completion_create_completion_callable",
    "api_v1_readiness_completion_smoke_plain_completion_llama_call_callable",
    "api_v1_readiness_completion_smoke_plain_completion_signature_inspectable",
    "api_v1_readiness_completion_smoke_plain_completion_accepts_prompt_kwarg",
    "api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg",
    "api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs",
    "api_v1_readiness_completion_smoke_plain_completion_reset_after_failure_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_error_category",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_token_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_attempted",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_ids",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_token_counts",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special_values",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_methods",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_categories",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_exception_types",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_safe_summaries",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_rejected_kwargs",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_result_shapes",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_tokenization_variants",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_count",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_attempted",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_supported",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_succeeded",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_rejected_kwarg",
    "api_v1_readiness_completion_smoke_qwen_high_level_chat_fallback_category",
    "api_v1_readiness_completion_smoke_plain_completion_eval_return_code",
    "api_v1_readiness_completion_smoke_plain_completion_first_failure_method",
    "api_v1_readiness_completion_smoke_plain_completion_backend_failure_category",
    "api_v1_readiness_completion_smoke_plain_completion_backend_state_sticky",
    "api_v1_readiness_completion_smoke_plain_completion_backend_recreation_required",
    "api_v1_readiness_completion_smoke_plain_completion_metal_error_category",
    "api_v1_readiness_completion_smoke_plain_completion_metal_command_buffer_status",
    "api_v1_readiness_qwen_64k_runtime_profile_id",
    "api_v1_readiness_qwen_64k_runtime_profile_attempt_ids",
    "api_v1_readiness_qwen_64k_runtime_profile_recovery_count",
    "api_v1_readiness_qwen_64k_runtime_profile_flash_attn",
    "api_v1_readiness_qwen_64k_runtime_profile_offload_kqv",
    "api_v1_readiness_qwen_64k_runtime_profile_type_k",
    "api_v1_readiness_qwen_64k_runtime_profile_type_v",
    "api_v1_readiness_qwen_64k_runtime_profile_n_batch",
    "api_v1_readiness_qwen_64k_runtime_profile_n_ubatch",
    "api_v1_readiness_qwen_64k_runtime_profile_result",
    "api_v1_readiness_qwen_64k_runtime_profile_failure_category",
    "api_v1_readiness_completion_smoke_qwen_api_v1_non_thinking_template_fallback",
];

fn is_safe_readiness_diagnostic_string(value: &str) -> bool {
    value.len() <= 256
        && value.chars().all(|ch| {
            ch.is_ascii_alphanumeric()
                || matches!(ch, '_' | '.' | ':' | '/' | '@' | ',' | '+' | '-')
        })
}

fn safe_readiness_diagnostics_from_payload(payload: &Value) -> Map<String, Value> {
    let mut safe = Map::new();
    let Some(map) = payload.as_object() else {
        return safe;
    };

    for key in SAFE_READINESS_DIAGNOSTIC_KEYS {
        let Some(value) = map.get(*key) else {
            continue;
        };
        let safe_value = match value {
            Value::Bool(_) | Value::Null => Some(value.clone()),
            Value::Number(number) if number.as_f64().is_some_and(f64::is_finite) => {
                Some(Value::Number(number.clone()))
            }
            Value::String(text) if is_safe_readiness_diagnostic_string(text) => {
                Some(Value::String(text.clone()))
            }
            _ => None,
        };
        if let Some(safe_value) = safe_value {
            safe.insert((*key).to_string(), safe_value);
        }
    }

    safe
}

fn startup_failure_status(
    request: &ComputeNodeRequest,
    last_error: String,
    operator_session_id: Option<String>,
    log_file_path: Option<String>,
) -> ComputeNodeStatus {
    ComputeNodeStatus {
        running: false,
        registered: false,
        active_relay_url: normalized_request_relay_urls(request)
            .first()
            .cloned()
            .unwrap_or_else(|| request.relay_base_url.clone()),
        configured_relay_urls: normalized_request_relay_urls(request),
        relay_statuses: Vec::new(),
        registered_relay_count: 0,
        configured_relay_count: normalized_request_relay_urls(request).len(),
        registered_relay_urls: Vec::new(),
        active_relay_urls: Vec::new(),
        requested_mode: format!("{:?}", request.mode).to_lowercase(),
        effective_mode: "cpu".into(),
        backend_available: "unknown".into(),
        backend_selected: "cpu".into(),
        backend_used: "cpu".into(),
        fallback_reason: None,
        model_path: request.model_path.clone(),
        last_error: Some(last_error),
        relay_runtime_state: Some("failed".into()),
        warm_load_state: Some("failed".into()),
        warm_load_enabled: Some(true),
        warm_load_duration_ms: None,
        context_tier: Some(normalize_context_tier(&request.context_tier)),
        context_window_tokens: context_profile(&normalize_context_tier(&request.context_tier))
            .map(|profile| profile.total_context_tokens),
        runtime_path: Some("bridge".into()),
        relay_runtime_path: Some("bridge".into()),
        worker_state: Some("failed".into()),
        worker_generation: None,
        worker_restart_count: None,
        worker_alive: Some(false),
        last_worker_error_code: None,
        last_worker_exit_code: None,
        last_worker_restart_at_ms: None,
        operator_session_id,
        sequence: None,
        updated_at_ms: Some(current_time_ms()),
        log_file_path,
        readiness_diagnostics: Map::new(),
    }
}

fn update_status_from_event(status: &mut ComputeNodeStatus, payload: &Value) -> bool {
    let payload_sequence = payload.get("sequence").and_then(Value::as_u64);
    let payload_session = event_session_id(payload);
    let is_fresh_start_event = payload.get("type").and_then(Value::as_str) == Some("started")
        && payload_sequence == Some(1)
        && !status.running
        && payload_session
            .is_some_and(|session| status.operator_session_id.as_deref() != Some(session));
    if let (Some(current_session), Some(payload_session)) =
        (status.operator_session_id.as_deref(), payload_session)
    {
        if current_session != payload_session && !is_fresh_start_event {
            return false;
        }
    }
    if let (Some(current_sequence), Some(payload_sequence)) = (status.sequence, payload_sequence) {
        if payload_sequence <= current_sequence && !is_fresh_start_event {
            return false;
        }
    }
    if let (Some(current_generation), Some(payload_generation)) = (
        status.worker_generation,
        payload.get("worker_generation").and_then(Value::as_u64),
    ) {
        if payload_generation < current_generation && !is_fresh_start_event {
            return false;
        }
    }
    if let Some(running) = payload.get("running").and_then(Value::as_bool) {
        status.running = running;
    }
    if payload.get("type").and_then(Value::as_str) == Some("started") {
        status.readiness_diagnostics.clear();
    }
    let readiness_diagnostics = safe_readiness_diagnostics_from_payload(payload);
    if matches!(
        payload.get("type").and_then(Value::as_str),
        Some("started" | "status" | "error" | "stopped")
    ) || !readiness_diagnostics.is_empty()
    {
        status.readiness_diagnostics = readiness_diagnostics;
    }
    if let Some(registered) = payload.get("registered").and_then(Value::as_bool) {
        status.registered = registered;
    }
    if let Some(active_relay_url) = payload.get("active_relay_url").and_then(Value::as_str) {
        status.active_relay_url = active_relay_url.into();
    }
    if let Some(configured_relay_urls) = payload
        .get("configured_relay_urls")
        .and_then(Value::as_array)
    {
        status.configured_relay_urls = configured_relay_urls
            .iter()
            .filter_map(Value::as_str)
            .map(ToOwned::to_owned)
            .collect();
    }
    if let Some(relay_statuses) = payload.get("relay_statuses").and_then(Value::as_array) {
        status.relay_statuses = relay_statuses.clone();
    }
    if let Some(registered_relay_count) = payload
        .get("registered_relay_count")
        .and_then(Value::as_u64)
    {
        status.registered_relay_count = registered_relay_count as usize;
    }
    if let Some(configured_relay_count) = payload
        .get("configured_relay_count")
        .and_then(Value::as_u64)
    {
        status.configured_relay_count = configured_relay_count as usize;
    }
    if let Some(registered_relay_urls) = payload
        .get("registered_relay_urls")
        .and_then(Value::as_array)
    {
        status.registered_relay_urls = registered_relay_urls
            .iter()
            .filter_map(Value::as_str)
            .map(ToOwned::to_owned)
            .collect();
    }
    if let Some(active_relay_urls) = payload.get("active_relay_urls").and_then(Value::as_array) {
        status.active_relay_urls = active_relay_urls
            .iter()
            .filter_map(Value::as_str)
            .map(ToOwned::to_owned)
            .collect();
    }
    if let Some(requested_mode) = payload.get("requested_mode").and_then(Value::as_str) {
        status.requested_mode = requested_mode.into();
    }
    if let Some(effective_mode) = payload.get("effective_mode").and_then(Value::as_str) {
        status.effective_mode = effective_mode.into();
    }
    if let Some(backend_available) = payload.get("backend_available").and_then(Value::as_str) {
        status.backend_available = backend_available.into();
    }
    if let Some(backend_selected) = payload.get("backend_selected").and_then(Value::as_str) {
        status.backend_selected = backend_selected.into();
    }
    if let Some(backend_used) = payload.get("backend_used").and_then(Value::as_str) {
        status.backend_used = backend_used.into();
    }
    if payload.get("fallback_reason").is_some() {
        status.fallback_reason = payload
            .get("fallback_reason")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if let Some(model_path) = payload.get("model_path").and_then(Value::as_str) {
        status.model_path = model_path.into();
    }
    if payload.get("last_error").is_some() {
        status.last_error = payload
            .get("last_error")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if payload.get("warm_load_state").is_some() {
        status.warm_load_state = payload
            .get("warm_load_state")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if payload.get("warm_load_enabled").is_some() {
        status.warm_load_enabled = payload.get("warm_load_enabled").and_then(Value::as_bool);
    }
    if payload.get("warm_load_duration_ms").is_some() {
        status.warm_load_duration_ms = payload.get("warm_load_duration_ms").and_then(Value::as_u64);
    }
    if payload.get("context_tier").is_some() {
        status.context_tier = payload
            .get("context_tier")
            .and_then(Value::as_str)
            .map(normalize_context_tier);
    }
    if payload.get("context_window_tokens").is_some() {
        status.context_window_tokens = payload
            .get("context_window_tokens")
            .and_then(Value::as_u64)
            .and_then(|value| u32::try_from(value).ok());
    }
    if payload.get("runtime_path").is_some() {
        status.runtime_path = payload
            .get("runtime_path")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if payload.get("relay_runtime_path").is_some() {
        status.relay_runtime_path = payload
            .get("relay_runtime_path")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if let Some(relay_runtime_state) = payload.get("relay_runtime_state").and_then(Value::as_str) {
        status.relay_runtime_state = Some(relay_runtime_state.into());
    }

    if payload.get("worker_state").is_some() {
        status.worker_state = payload
            .get("worker_state")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if let Some(worker_generation) = payload.get("worker_generation").and_then(Value::as_u64) {
        status.worker_generation = Some(worker_generation);
    }
    if payload.get("worker_restart_count").is_some() {
        status.worker_restart_count = payload.get("worker_restart_count").and_then(Value::as_u64);
    }
    if payload.get("worker_alive").is_some() {
        status.worker_alive = payload.get("worker_alive").and_then(Value::as_bool);
    }
    if payload.get("last_worker_error_code").is_some() {
        status.last_worker_error_code = payload
            .get("last_worker_error_code")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if payload.get("last_worker_exit_code").is_some() {
        status.last_worker_exit_code = payload.get("last_worker_exit_code").and_then(Value::as_i64);
    }
    if payload.get("last_worker_restart_at_ms").is_some() {
        status.last_worker_restart_at_ms = payload
            .get("last_worker_restart_at_ms")
            .and_then(Value::as_u64);
    }
    if let Some(operator_session_id) = payload.get("operator_session_id").and_then(Value::as_str) {
        status.operator_session_id = Some(operator_session_id.into());
    }
    if let Some(sequence) = payload.get("sequence").and_then(Value::as_u64) {
        status.sequence = Some(sequence);
    }
    if let Some(updated_at_ms) = payload.get("updated_at_ms").and_then(Value::as_u64) {
        status.updated_at_ms = Some(updated_at_ms);
    } else {
        status.updated_at_ms = Some(current_time_ms());
    }
    if payload.get("log_file_path").is_some() {
        status.log_file_path = payload
            .get("log_file_path")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if payload.get("type").and_then(Value::as_str) == Some("error") {
        status.last_error = payload
            .get("message")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
            .or_else(|| Some("compute-node bridge error".into()));
    }
    true
}

fn bridge_exit_status_label(exit_status: std::process::ExitStatus) -> String {
    if let Some(code) = exit_status.code() {
        return code.to_string();
    }
    #[cfg(unix)]
    if let Some(signal) = exit_status.signal() {
        return format!("signal {signal}");
    }
    format!("{exit_status:?}")
}

fn bridge_exit_error(
    exit_status: std::process::ExitStatus,
    saw_startup_event: bool,
    diagnostic_tail: Option<&str>,
) -> Option<String> {
    let status_label = bridge_exit_status_label(exit_status);
    let diagnostic_suffix = diagnostic_tail
        .and_then(last_compute_node_stderr_diagnostic)
        .map(|line| format!("; recent diagnostic: {line}"))
        .unwrap_or_default();
    if !saw_startup_event {
        return Some(format!(
            "compute-node bridge exited with status {status_label} before emitting a startup \
             event; see desktop.compute_node.stderr logs{diagnostic_suffix}"
        ));
    }
    if exit_status.success() {
        return None;
    }
    Some(format!(
        "compute-node bridge exited with status {status_label}; \
         see desktop.compute_node.stderr logs{diagnostic_suffix}"
    ))
}

fn last_compute_node_stderr_diagnostic(tail: &str) -> Option<String> {
    tail.lines()
        .rev()
        .find(|line| line.split_whitespace().nth(1) == Some("desktop.compute_node.stderr"))
        .map(|line| sanitize_operator_diagnostic_line(line).trim().to_string())
        .filter(|line| !line.is_empty())
}

fn recent_operator_log_tail(log_file_path: Option<&str>) -> Option<String> {
    log_file_path
        .and_then(|path| read_log_tail(Path::new(path), 4096).ok())
        .filter(|tail| !tail.trim().is_empty())
}

fn finalize_bridge_exit(
    status: &mut ComputeNodeStatus,
    exit_status: std::process::ExitStatus,
    saw_startup_event: bool,
    saw_error_event: bool,
    expected_session_id: &str,
    fallback_log_file_path: Option<&str>,
) -> Option<Value> {
    if status.operator_session_id.as_deref() != Some(expected_session_id) {
        return None;
    }

    status.running = false;
    status.registered = false;
    status.registered_relay_count = 0;
    status.registered_relay_urls.clear();
    status.active_relay_urls.clear();
    let preserve_failed_state = status.relay_runtime_state.as_deref() == Some("failed")
        || status.warm_load_state.as_deref() == Some("failed");
    if preserve_failed_state {
        status.relay_runtime_state = Some("failed".into());
    } else {
        status.relay_runtime_state = Some("stopped".into());
    }

    let should_read_recent_tail = !exit_status.success() || !saw_startup_event;
    let recent_tail = should_read_recent_tail
        .then(|| {
            recent_operator_log_tail(status.log_file_path.as_deref().or(fallback_log_file_path))
        })
        .flatten();
    let exit_error = bridge_exit_error(exit_status, saw_startup_event, recent_tail.as_deref());
    if status.last_error.is_none() {
        status.last_error = exit_error.clone();
    }

    if saw_error_event {
        return None;
    }

    exit_error.map(|last_error| {
        let sequence = status.sequence.unwrap_or(0).saturating_add(1);
        let updated_at_ms = current_time_ms();
        status.sequence = Some(sequence);
        status.updated_at_ms = Some(updated_at_ms);
        serde_json::json!({
            "type": "error",
            "running": false,
            "registered": false,
            "registered_relay_count": 0,
            "registered_relay_urls": [],
            "active_relay_urls": [],
            "relay_runtime_state": status.relay_runtime_state.as_deref().unwrap_or("stopped"),
            "last_error": last_error,
            "message": last_error,
            "operator_session_id": expected_session_id,
            "sequence": sequence,
            "updated_at_ms": updated_at_ms,
            "readiness_diagnostics": status.readiness_diagnostics.clone(),
        })
    })
}

async fn drain_compute_node_stderr<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
    policy: SubprocessLogPolicy,
    log_sink: Option<OperatorLogSink>,
) -> anyhow::Result<()> {
    let mut lines = BufReader::new(reader).lines();
    let mut filter = SubprocessLogFilter::new("compute_node", policy);
    while let Some(line) = lines.next_line().await? {
        append_operator_log_line(
            &log_sink,
            "desktop.compute_node.stderr",
            &sanitize_operator_diagnostic_line(&line),
        );
        if filter.should_emit(&line) {
            eprintln!("desktop.compute_node.stderr line={line}");
        }
    }
    Ok(())
}

fn append_operator_log_line(log_sink: &Option<OperatorLogSink>, source: &str, line: &str) {
    if let Some(log_sink) = log_sink {
        log_sink.append_line(source, line);
    }
}

fn bridge_session_env_vars(
    session_id: &str,
    log_file_path: Option<&str>,
) -> Vec<(&'static str, String)> {
    let mut env_vars = vec![("TOKENPLACE_COMPUTE_NODE_SESSION_ID", session_id.to_string())];
    if let Some(path) = log_file_path {
        env_vars.push(("TOKENPLACE_OPERATOR_LOG_FILE", path.to_string()));
    }
    env_vars
}

fn append_operator_log_path_line(log_file_path: Option<&str>, source: &str, line: &str) {
    if let Some(log_file_path) = log_file_path {
        let _ = append_line_to_path(Path::new(log_file_path), source, line);
    }
}

fn redact_bridge_stdout_line(line: &str) -> String {
    let Ok(payload) = serde_json::from_str::<Value>(line) else {
        return sanitize_freeform_bridge_log_line(line);
    };
    summarize_bridge_stdout_payload(&payload)
}

fn summarize_bridge_stdout_payload(payload: &Value) -> String {
    let mut summary = serde_json::Map::new();
    let Some(map) = payload.as_object() else {
        return "{\"type\":\"non_object_bridge_event\"}".into();
    };

    for key in [
        "type",
        "operator_session_id",
        "sequence",
        "updated_at_ms",
        "running",
        "registered",
        "relay_runtime_state",
        "warm_load_state",
        "warm_load_enabled",
        "warm_load_duration_ms",
        "context_tier",
        "context_window_tokens",
        "requested_mode",
        "effective_mode",
        "backend_available",
        "backend_selected",
        "backend_used",
        "fallback_reason",
        "runtime_path",
        "relay_runtime_path",
        "interpreter",
        "llama_module_path",
        "runtime_action",
        "runtime_setup_message",
        "code",
        "message",
        "last_error",
        "configured_relay_count",
        "registered_relay_count",
        "api_v1_readiness_result",
        "api_v1_readiness_error_code",
        "api_v1_readiness_error_reason",
        "api_v1_readiness_qwen_64k_runtime_profile_id",
        "api_v1_readiness_qwen_64k_runtime_profile_result",
        "api_v1_readiness_qwen_64k_runtime_profile_recovery_count",
        "api_v1_readiness_qwen_64k_first_readiness_failure_backend_failure_category",
        "api_v1_readiness_qwen_64k_first_readiness_failure_metal_error_category",
        "api_v1_readiness_completion_smoke_plain_completion_backend_failure_category",
        "api_v1_readiness_completion_smoke_plain_completion_metal_error_category",
    ] {
        if let Some(value) = map.get(key) {
            summary.insert(key.to_string(), sanitize_bridge_log_value(key, value));
        }
    }

    for key in ["active_relay_url", "relay_base_url", "relay_url"] {
        if let Some(value) = map.get(key).and_then(Value::as_str) {
            summary.insert(key.to_string(), Value::String(sanitize_relay_target(value)));
        }
    }

    let serialized = serde_json::to_string(&Value::Object(summary))
        .unwrap_or_else(|_| "{\"type\":\"bridge_event_summary_error\"}".into());
    if serialized.len() <= 3500 {
        serialized
    } else {
        serde_json::to_string(&serde_json::json!({
            "type": payload.get("type").and_then(Value::as_str).unwrap_or("bridge_event"),
            "operator_session_id": payload.get("operator_session_id").and_then(Value::as_str).unwrap_or("unknown"),
            "sequence": payload.get("sequence").and_then(Value::as_u64).unwrap_or(0),
            "summary_truncated": true,
        }))
        .unwrap_or_else(|_| "{\"type\":\"bridge_event_summary_error\"}".into())
    }
}

fn readiness_operator_log_chunks(payload: &Value) -> Vec<String> {
    let diagnostics = safe_readiness_diagnostics_from_payload(payload);
    if diagnostics.is_empty() {
        return Vec::new();
    }
    let operator_session_id = payload
        .get("operator_session_id")
        .and_then(Value::as_str)
        .unwrap_or("unknown")
        .to_string();
    let sequence = payload.get("sequence").and_then(Value::as_u64).unwrap_or(0);
    let mut chunks: Vec<Map<String, Value>> = Vec::new();
    let mut current = Map::new();
    let mut current_len = 0usize;
    let mut sorted = BTreeMap::new();
    for (key, value) in diagnostics {
        sorted.insert(key, value);
    }
    for (key, value) in sorted {
        let mut safe_value = value;
        let mut pair_len = serde_json::to_string(&serde_json::json!({ &key: safe_value.clone() }))
            .map(|text| text.len())
            .unwrap_or(3500);
        if pair_len > 2800 {
            safe_value = serde_json::json!({
                "omitted": true,
                "reason": "diagnostic_value_too_large"
            });
            pair_len = serde_json::to_string(&serde_json::json!({ &key: safe_value.clone() }))
                .map(|text| text.len())
                .unwrap_or(128);
        }
        if !current.is_empty() && current_len.saturating_add(pair_len) > 2800 {
            chunks.push(current);
            current = Map::new();
            current_len = 0;
        }
        current_len = current_len.saturating_add(pair_len);
        current.insert(key, safe_value);
    }
    if !current.is_empty() {
        chunks.push(current);
    }
    let chunk_count = chunks.len();
    chunks
        .into_iter()
        .enumerate()
        .map(|(idx, diagnostics)| {
            let event = serde_json::json!({
                "type": "readiness_diagnostics",
                "operator_session_id": operator_session_id,
                "sequence": sequence,
                "chunk_index": idx,
                "chunk_count": chunk_count,
                "diagnostics": diagnostics,
            });
            let serialized = serde_json::to_string(&event)
                .unwrap_or_else(|_| "{\"type\":\"readiness_diagnostics_error\"}".into());
            if serialized.len() <= 3500 {
                serialized
            } else {
                serde_json::to_string(&serde_json::json!({
                    "type": "readiness_diagnostics",
                    "operator_session_id": operator_session_id,
                    "sequence": sequence,
                    "chunk_index": idx,
                    "chunk_count": chunk_count,
                    "diagnostics": {
                        "chunk_omitted": {
                            "omitted": true,
                            "reason": "diagnostic_chunk_too_large"
                        }
                    },
                }))
                .unwrap_or_else(|_| "{\"type\":\"readiness_diagnostics_error\"}".into())
            }
        })
        .collect()
}
fn sanitize_bridge_log_value(key: &str, value: &Value) -> Value {
    if is_sensitive_bridge_log_key(key) {
        return Value::String("<redacted>".into());
    }
    match value {
        Value::String(text) => Value::String(sanitize_freeform_bridge_log_line(text)),
        Value::Bool(_) | Value::Number(_) | Value::Null => value.clone(),
        Value::Array(items) => Value::Array(
            items
                .iter()
                .take(8)
                .map(|item| sanitize_bridge_log_value(key, item))
                .collect(),
        ),
        Value::Object(_) => Value::String("<object omitted>".into()),
    }
}

fn is_sensitive_bridge_log_key(key: &str) -> bool {
    let normalized = key.to_ascii_lowercase();
    normalized.contains("prompt")
        || normalized.contains("response")
        || normalized.contains("tool")
        || normalized.contains("private_key")
        || normalized.contains("decrypted")
        || normalized.contains("payload")
        || normalized == "model_path"
}

fn sanitize_freeform_bridge_log_line(line: &str) -> String {
    sanitize_operator_diagnostic_line(line)
}

fn sanitize_path_for_operator_log(path: &Path) -> String {
    sanitize_operator_path_display(path)
}

fn with_log_file_path(mut payload: Value, log_file_path: Option<&str>) -> Value {
    if let Value::Object(map) = &mut payload {
        match log_file_path {
            Some(path) => {
                map.insert("log_file_path".into(), Value::String(path.to_string()));
            }
            None => {
                map.insert("log_file_path".into(), Value::Null);
            }
        }
    }
    payload
}

pub async fn start_compute_node(
    app: AppHandle,
    state: ComputeNodeState,
    request: ComputeNodeRequest,
) -> anyhow::Result<()> {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let relay_base_urls = normalized_request_relay_urls(&request);
    let primary_relay_url = relay_base_urls
        .first()
        .cloned()
        .unwrap_or_else(|| request.relay_base_url.clone());

    {
        let _lifecycle_lock = state.lifecycle_lock.lock().await;
        let mut child_slot = state.child.lock().await;
        if child_slot
            .as_mut()
            .is_some_and(|child| child.try_wait().ok().flatten().is_none())
        {
            anyhow::bail!("compute node already running; stop it before starting a new session");
        }
        *child_slot = None;
        *state.stdin.lock().await = None;
    }

    let session_id = {
        let mut next_session_id = state.next_session_id.lock().await;
        *next_session_id += 1;
        next_session_id.to_string()
    };
    {
        let mut status = state.status.lock().await;
        status.operator_session_id = Some(session_id.clone());
        status.log_file_path = None;
        status.last_error = None;
        status.updated_at_ms = Some(current_time_ms());
    }
    let log_sink = match OperatorLogSink::create(&app, &session_id) {
        Ok(log_sink) => Some(log_sink),
        Err(err) => {
            eprintln!(
                "desktop.compute_node.operator_log_create_error operator_session_id={} error={}",
                session_id, err
            );
            None
        }
    };
    let log_file_path = log_sink
        .as_ref()
        .map(|log_sink| log_sink.path().to_string_lossy().into_owned());
    append_operator_log_line(
        &log_sink,
        "desktop.compute_node.session.start",
        &format!(
            "operator_session_id={} relay=<redacted> model_path=<redacted> requested_mode={}",
            session_id,
            format!("{:?}", request.mode).to_lowercase()
        ),
    );
    if std::env::var("TOKEN_PLACE_DESKTOP_OPEN_DEBUG_TERMINAL")
        .ok()
        .as_deref()
        == Some("1")
    {
        if let Some(log_sink) = &log_sink {
            if let Err(err) = crate::operator_logs::open_debug_terminal(log_sink.path()) {
                log_sink.append_line(
                    "desktop.compute_node.debug_terminal_error",
                    &format!("open failed: {err}"),
                );
            }
        }
    }

    let bridge_script = match resolve_bridge_script(&app) {
        Ok(bridge_script) => bridge_script,
        Err(err) => {
            {
                let mut status = state.status.lock().await;
                *status = startup_failure_status(
                    &request,
                    err.clone(),
                    Some(session_id.clone()),
                    log_file_path.clone(),
                );
            }
            return Err(anyhow::anyhow!(err));
        }
    };
    let launcher = if is_python_script(&bridge_script) {
        let resource_dir = app.path().resource_dir().ok();
        let current_exe = std::env::current_exe().ok();
        match tokio::task::spawn_blocking(move || {
            resolve_python_launcher_resource_aware(PythonLauncherResolutionOptions {
                override_var_name: "TOKEN_PLACE_SIDECAR_PYTHON",
                tauri_resource_dir: resource_dir.as_deref(),
                current_exe_path: current_exe.as_deref(),
                manifest_dir: Path::new(env!("CARGO_MANIFEST_DIR")),
                packaged: !cfg!(debug_assertions),
            })
        })
        .await
        {
            Ok(result) => match result {
                Ok(launcher) => Some(launcher),
                Err(err) => {
                    {
                        let mut status = state.status.lock().await;
                        *status = startup_failure_status(
                            &request,
                            err.to_string(),
                            Some(session_id.clone()),
                            log_file_path.clone(),
                        );
                    }
                    return Err(err.into());
                }
            },
            Err(err) => {
                let err = anyhow::anyhow!("python launcher resolver task failed: {err}");
                {
                    let mut status = state.status.lock().await;
                    *status = startup_failure_status(
                        &request,
                        err.to_string(),
                        Some(session_id.clone()),
                        log_file_path.clone(),
                    );
                }
                return Err(err.into());
            }
        }
    } else {
        None
    };

    let mut bridge_command = match build_bridge_command(&bridge_script, launcher) {
        Ok(command) => command,
        Err(err) => {
            {
                let mut status = state.status.lock().await;
                *status = startup_failure_status(
                    &request,
                    err.to_string(),
                    Some(session_id.clone()),
                    log_file_path.clone(),
                );
            }
            return Err(err.into());
        }
    };
    let exe_path = std::env::current_exe().ok();
    let resource_dir = app.path().resource_dir().ok();
    let (selected_resource_root, selected_layout) = describe_resource_layout(
        Path::new(&bridge_script),
        exe_path.as_deref(),
        manifest_dir,
        resource_dir.as_deref(),
    );
    let import_root =
        configure_runtime_pythonpath(&mut bridge_command, manifest_dir, &bridge_script);
    let interpreter = bridge_command
        .as_std()
        .get_program()
        .to_string_lossy()
        .into_owned();
    eprintln!(
        "desktop.compute_node.session.start operator_session_id={} relay={} bridge={} interpreter={} resource_root={} layout={:?} import_root={} cancellation_token_reset=true",
        session_id,
        sanitize_relay_target(&primary_relay_url),
        bridge_script,
        interpreter,
        selected_resource_root.display(),
        selected_layout,
        import_root.as_deref().map(|p| p.display().to_string()).unwrap_or_else(|| "<unresolved>".into())
    );
    append_operator_log_line(
        &log_sink,
        "desktop.compute_node.session.layout",
        &format!(
            "operator_session_id={} relay={} bridge={} interpreter={} resource_root={} layout={:?} import_root={}",
            session_id,
            sanitize_relay_target(&primary_relay_url),
            sanitize_path_for_operator_log(Path::new(&bridge_script)),
            sanitize_freeform_bridge_log_line(&interpreter),
            sanitize_path_for_operator_log(&selected_resource_root),
            selected_layout,
            import_root
                .as_ref()
                .map(|path| sanitize_path_for_operator_log(path))
                .unwrap_or_else(|| "<unresolved>".into())
        ),
    );
    configure_runtime_bootstrap_env(&mut bridge_command, &request.mode);
    for (key, value) in bridge_session_env_vars(&session_id, log_file_path.as_deref()) {
        bridge_command.env(key, value);
    }

    let spawn_result = bridge_command
        .arg("--model")
        .arg(&request.model_path)
        .arg("--mode")
        .arg(format!("{:?}", request.mode).to_lowercase())
        .arg("--context-tier")
        .arg(normalize_context_tier(&request.context_tier))
        .args(
            relay_base_urls
                .iter()
                .flat_map(|relay_url| ["--relay-url", relay_url.as_str()]),
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();

    let mut child = match spawn_result {
        Ok(child) => child,
        Err(err) => {
            {
                let _lifecycle_lock = state.lifecycle_lock.lock().await;
                let mut status = state.status.lock().await;
                *status = startup_failure_status(
                    &request,
                    format!("failed to start compute-node bridge: {err}"),
                    Some(session_id.clone()),
                    log_file_path.clone(),
                );
                *state.child.lock().await = None;
                *state.stdin.lock().await = None;
            }
            anyhow::bail!("failed to spawn compute-node bridge: {err}");
        }
    };

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute-node bridge stdout"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute-node bridge stderr"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute-node bridge stdin"))?;

    let mut pending_child = Some(child);
    let mut pending_stdin = Some(stdin);
    let installed = {
        let _lifecycle_lock = state.lifecycle_lock.lock().await;
        let mut child_slot = state.child.lock().await;
        if child_slot
            .as_mut()
            .is_some_and(|existing| existing.try_wait().ok().flatten().is_none())
        {
            false
        } else {
            eprintln!(
                "desktop.compute_node.bridge_process.spawned operator_session_id={} relay={}",
                session_id,
                sanitize_relay_target(&primary_relay_url)
            );
            *child_slot = pending_child.take();
            let mut stdin_slot = state.stdin.lock().await;
            *stdin_slot = pending_stdin.take();
            let mut status = state.status.lock().await;
            *status = ComputeNodeStatus {
                running: true,
                registered: false,
                active_relay_url: primary_relay_url.clone(),
                configured_relay_urls: relay_base_urls.clone(),
                relay_statuses: Vec::new(),
                registered_relay_count: 0,
                configured_relay_count: relay_base_urls.len(),
                registered_relay_urls: Vec::new(),
                active_relay_urls: Vec::new(),
                requested_mode: format!("{:?}", request.mode).to_lowercase(),
                effective_mode: "cpu".into(),
                backend_available: "unknown".into(),
                backend_selected: "cpu".into(),
                backend_used: "cpu".into(),
                fallback_reason: None,
                model_path: request.model_path.clone(),
                last_error: None,
                relay_runtime_state: Some("starting".into()),
                warm_load_state: Some("not_started".into()),
                warm_load_enabled: Some(true),
                warm_load_duration_ms: None,
                context_tier: Some(normalize_context_tier(&request.context_tier)),
                context_window_tokens: context_profile(&normalize_context_tier(
                    &request.context_tier,
                ))
                .map(|profile| profile.total_context_tokens),
                runtime_path: Some("bridge".into()),
                relay_runtime_path: Some("bridge".into()),
                worker_state: Some("starting".into()),
                worker_generation: None,
                worker_restart_count: None,
                worker_alive: Some(false),
                last_worker_error_code: None,
                last_worker_exit_code: None,
                last_worker_restart_at_ms: None,
                operator_session_id: Some(session_id.clone()),
                sequence: Some(0),
                updated_at_ms: Some(current_time_ms()),
                log_file_path: log_file_path.clone(),
                readiness_diagnostics: Map::new(),
            };
            true
        }
    };

    if !installed {
        if let Some(mut abandoned_stdin) = pending_stdin.take() {
            let _ = abandoned_stdin.write_all(b"{\"type\":\"cancel\"}\n").await;
            let _ = abandoned_stdin.flush().await;
        }
        if let Some(mut abandoned_child) = pending_child.take() {
            let _ = abandoned_child.kill().await;
            let _ = abandoned_child.wait().await;
        }
        anyhow::bail!("compute node already running; stop it before starting a new session");
    }

    let log_policy = SubprocessLogPolicy::from_env();
    let stderr_log_sink = log_sink.clone();
    let stderr_task = tokio::spawn(async move {
        if let Err(err) = drain_compute_node_stderr(stderr, log_policy, stderr_log_sink).await {
            eprintln!("desktop.compute_node.stderr_error error={err}");
        }
    });

    let mut lines = BufReader::new(stdout).lines();
    let mut saw_error_event = false;
    let mut saw_startup_event = false;
    while let Some(line) = lines.next_line().await? {
        let parsed_for_log = serde_json::from_str::<Value>(&line).ok();
        append_operator_log_line(
            &log_sink,
            "desktop.compute_node.stdout",
            &redact_bridge_stdout_line(&line),
        );
        if let Some(payload) = parsed_for_log.as_ref() {
            for chunk in readiness_operator_log_chunks(payload) {
                append_operator_log_line(&log_sink, "desktop.compute_node.readiness", &chunk);
            }
        }
        match parse_compute_node_event_line(&line) {
            Ok(payload) => {
                let payload = with_log_file_path(payload, log_file_path.as_deref());
                if let Some(event_type) = payload.get("type").and_then(Value::as_str) {
                    if event_type == "error" {
                        saw_error_event = true;
                    }
                    if event_type == "started" || event_type == "status" {
                        saw_startup_event = true;
                    }
                }
                {
                    let mut status = state.status.lock().await;
                    if !update_status_from_event(&mut status, &payload) {
                        continue;
                    }
                }
                app.emit("compute_node_event", payload)?;
            }
            Err(err) => {
                eprintln!(
                    "desktop.compute_node.stdout_parse_error error={} line={}",
                    err, line
                );
            }
        }
    }

    if let Err(err) = stderr_task.await {
        eprintln!("desktop.compute_node.stderr_task_join_error error={err}");
    }

    let running_child = {
        let _lifecycle_lock = state.lifecycle_lock.lock().await;
        let current_session = state.status.lock().await.operator_session_id.clone();
        if current_session.as_deref() == Some(session_id.as_str()) {
            let mut child_slot = state.child.lock().await;
            child_slot.take()
        } else {
            None
        }
    };

    if let Some(mut running_child) = running_child {
        let exit_status = running_child.wait().await?;
        let exit_payload = {
            let mut status = state.status.lock().await;
            finalize_bridge_exit(
                &mut status,
                exit_status,
                saw_startup_event,
                saw_error_event,
                &session_id,
                log_file_path.as_deref(),
            )
        };

        append_operator_log_line(
            &log_sink,
            "desktop.compute_node.bridge_process_exited",
            &format!(
                "operator_session_id={} status={}",
                session_id,
                bridge_exit_status_label(exit_status)
            ),
        );

        if let Some(payload) = exit_payload {
            app.emit("compute_node_event", payload)?;
        }
    } else {
        let mut status = state.status.lock().await;
        if status.operator_session_id.as_deref() == Some(session_id.as_str()) {
            status.running = false;
            status.registered = false;
        }
    }
    {
        let _lifecycle_lock = state.lifecycle_lock.lock().await;
        let current_session = state.status.lock().await.operator_session_id.clone();
        if current_session.as_deref() == Some(session_id.as_str()) {
            *state.stdin.lock().await = None;
        }
    }

    Ok(())
}

pub async fn stop_compute_node(state: ComputeNodeState) -> anyhow::Result<()> {
    let (stop_session_id, stop_log_file_path) = {
        let status = state.status.lock().await;
        (
            status.operator_session_id.clone(),
            status.log_file_path.clone(),
        )
    };
    eprintln!(
        "desktop.compute_node.stop_requested operator_session_id={}",
        stop_session_id.as_deref().unwrap_or("unknown")
    );
    append_operator_log_path_line(
        stop_log_file_path.as_deref(),
        "desktop.compute_node.stop_requested",
        &format!(
            "operator_session_id={}",
            stop_session_id.as_deref().unwrap_or("unknown")
        ),
    );
    let mut stdin_handle = {
        let mut stdin_lock = state.stdin.lock().await;
        stdin_lock.take()
    };
    if let Some(stdin) = stdin_handle.as_mut() {
        eprintln!(
            "desktop.compute_node.cancel_requested operator_session_id={}",
            stop_session_id.as_deref().unwrap_or("unknown")
        );
        append_operator_log_path_line(
            stop_log_file_path.as_deref(),
            "desktop.compute_node.cancel_requested",
            &format!(
                "operator_session_id={}",
                stop_session_id.as_deref().unwrap_or("unknown")
            ),
        );
        let _ = stdin.write_all(b"{\"type\":\"cancel\"}\n").await;
        let _ = stdin.flush().await;
    }

    let mut owned_child = None;
    for _ in 0..20 {
        if let Some(child) = state.child.lock().await.take() {
            owned_child = Some(child);
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }

    if let Some(mut child) = owned_child {
        let shutdown_deadline = Duration::from_secs(12);
        let shutdown_started = Instant::now();
        let mut exited = child.try_wait()?.is_some();
        while !exited && shutdown_started.elapsed() < shutdown_deadline {
            tokio::time::sleep(Duration::from_millis(50)).await;
            exited = child.try_wait()?.is_some();
        }

        if !exited {
            eprintln!(
                "desktop.compute_node.bridge_kill_requested operator_session_id={}",
                stop_session_id.as_deref().unwrap_or("unknown")
            );
            append_operator_log_path_line(
                stop_log_file_path.as_deref(),
                "desktop.compute_node.bridge_kill_requested",
                &format!(
                    "operator_session_id={}",
                    stop_session_id.as_deref().unwrap_or("unknown")
                ),
            );
            let _ = child.kill().await;
            let _ = child.wait().await;
            eprintln!(
                "desktop.compute_node.bridge_process_exited operator_session_id={} killed=true",
                stop_session_id.as_deref().unwrap_or("unknown")
            );
            append_operator_log_path_line(
                stop_log_file_path.as_deref(),
                "desktop.compute_node.bridge_process_exited",
                &format!(
                    "operator_session_id={} killed=true",
                    stop_session_id.as_deref().unwrap_or("unknown")
                ),
            );
        } else {
            eprintln!(
                "desktop.compute_node.bridge_process_exited operator_session_id={} killed=false",
                stop_session_id.as_deref().unwrap_or("unknown")
            );
            append_operator_log_path_line(
                stop_log_file_path.as_deref(),
                "desktop.compute_node.bridge_process_exited",
                &format!(
                    "operator_session_id={} killed=false",
                    stop_session_id.as_deref().unwrap_or("unknown")
                ),
            );
        }
    }

    {
        let mut status = state.status.lock().await;
        status.running = false;
        status.registered = false;
        status.registered_relay_count = 0;
        status.registered_relay_urls.clear();
        status.active_relay_urls.clear();
        status.relay_runtime_state = Some("stopped".into());
        status.last_error = None;
        status.updated_at_ms = Some(current_time_ms());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command as StdCommand;
    use std::process::ExitStatus;
    use tempfile::TempDir;
    use tokio::io::AsyncBufReadExt;
    use tokio::process::Command;

    fn success_exit_status() -> ExitStatus {
        #[cfg(windows)]
        {
            StdCommand::new("cmd")
                .args(["/C", "exit", "0"])
                .status()
                .expect("status")
        }
        #[cfg(not(windows))]
        {
            StdCommand::new("sh")
                .args(["-c", "exit 0"])
                .status()
                .expect("status")
        }
    }

    #[test]
    fn compute_node_request_accepts_multiple_relay_urls() {
        let request: ComputeNodeRequest = serde_json::from_str(
            r#"{
                "model_path": "/tmp/model.gguf",
                "relay_base_url": "https://token.place",
                "relay_base_urls": ["https://token.place", "https://staging.token.place"],
                "mode": "cpu"
            }"#,
        )
        .expect("request should deserialize");

        assert_eq!(request.context_tier, "8k-fast");

        assert_eq!(
            normalized_request_relay_urls(&request),
            vec![
                "https://token.place".to_string(),
                "https://staging.token.place".to_string(),
            ]
        );
    }

    #[test]
    fn compute_node_request_serializes_context_tier() {
        let request = ComputeNodeRequest {
            model_path: "/tmp/model.gguf".into(),
            relay_base_url: "https://token.place".into(),
            relay_base_urls: vec!["https://token.place".into()],
            mode: ComputeMode::Cpu,
            context_tier: "64k-full".into(),
        };

        let payload = serde_json::to_value(&request).expect("serialize request");

        assert_eq!(
            payload.get("context_tier").and_then(Value::as_str),
            Some("64k-full")
        );
    }

    #[test]
    fn update_status_from_event_preserves_multi_relay_status_fields() {
        let mut status = ComputeNodeStatus::default();
        let payload = serde_json::json!({
            "type": "status",
            "running": true,
            "registered": true,
            "active_relay_url": "https://token.place",
            "configured_relay_urls": ["https://token.place", "https://staging.token.place"],
            "registered_relay_count": 1,
            "configured_relay_count": 2,
            "registered_relay_urls": ["https://token.place"],
            "active_relay_urls": ["https://token.place"],
            "relay_statuses": [
                {"relay_url": "https://token.place", "registered": true, "relay_runtime_state": "ready", "last_error": null},
                {"relay_url": "https://staging.token.place", "registered": false, "relay_runtime_state": "ready", "last_error": "timeout"}
            ]
        });

        assert!(update_status_from_event(&mut status, &payload));

        assert!(status.registered);
        assert_eq!(status.registered_relay_count, 1);
        assert_eq!(status.configured_relay_count, 2);
        assert_eq!(
            status.configured_relay_urls,
            vec![
                "https://token.place".to_string(),
                "https://staging.token.place".to_string(),
            ]
        );
        assert_eq!(
            status.registered_relay_urls,
            vec!["https://token.place".to_string()]
        );
        assert_eq!(
            status.active_relay_urls,
            vec!["https://token.place".to_string()]
        );
        assert_eq!(status.relay_statuses.len(), 2);
    }

    #[test]
    fn compute_bridge_disables_user_site_when_import_root_is_unresolved() {
        let temp = TempDir::new().expect("tempdir");
        let bridge = temp.path().join("python").join("compute_node_bridge.py");
        std::fs::create_dir_all(bridge.parent().expect("bridge parent"))
            .expect("create bridge dir");
        std::fs::write(&bridge, "print('ok')\n").expect("write bridge");
        let manifest_dir = temp.path().join("missing-manifest");
        let mut command = Command::new("python");

        let import_root = configure_runtime_pythonpath(
            &mut command,
            &manifest_dir,
            bridge.to_str().expect("bridge path should be UTF-8"),
        );

        assert!(import_root.is_none());
        assert_eq!(
            command_env_value(&command, "PYTHONNOUSERSITE").as_deref(),
            Some("1")
        );
        assert!(command_env_value(&command, "PYTHONPATH").is_none());
    }

    #[test]
    fn sanitize_relay_target_strips_userinfo_query_and_fragment() {
        assert_eq!(
            sanitize_relay_target("https://user:pass@example.com/path?token=secret#frag"),
            "https://example.com"
        );
    }

    #[test]
    fn redacts_sensitive_bridge_stdout_fields_before_operator_logging() {
        let line = serde_json::json!({
            "type": "status",
            "operator_session_id": "1",
            "sequence": 7,
            "model_path": "/Users/Example User/models/model.gguf",
            "active_relay_url": "https://relay.internal.example/path?token=secret",
            "prompt": "plaintext prompt",
            "tool_args": {"private_key": "secret"},
            "backend_selected": "cpu",
            "relay_runtime_state": "ready"
        })
        .to_string();

        let redacted = redact_bridge_stdout_line(&line);
        let payload: Value = serde_json::from_str(&redacted).expect("summary json");

        assert_eq!(payload.get("type").and_then(Value::as_str), Some("status"));
        assert_eq!(
            payload.get("operator_session_id").and_then(Value::as_str),
            Some("1")
        );
        assert_eq!(payload.get("sequence").and_then(Value::as_u64), Some(7));
        assert_eq!(
            payload.get("backend_selected").and_then(Value::as_str),
            Some("cpu")
        );
        assert_eq!(
            payload.get("active_relay_url").and_then(Value::as_str),
            Some("https://relay.internal.example")
        );
        assert!(payload.get("model_path").is_none());
        assert!(payload.get("prompt").is_none());
        assert!(payload.get("tool_args").is_none());
        assert!(!redacted.contains("Example User"));
        assert!(!redacted.contains("plaintext prompt"));
        assert!(!redacted.contains("token=secret"));
    }

    #[test]
    fn summarize_bridge_stdout_payload_excludes_readiness_diagnostics() {
        let summary = summarize_bridge_stdout_payload(&serde_json::json!({
            "type": "error",
            "last_error": "runtime_completion_smoke_plain_completion_worker_exception",
            "api_v1_readiness_completion_smoke_method": "create_completion_keyword_prompt",
            "api_v1_readiness_completion_smoke_generation_exception_category": "worker_exception",
            "api_v1_readiness_completion_smoke_exception_type": "LlamaCppInferenceRequestError",
            "api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg": true,
            "api_v1_readiness_completion_smoke_attempted_plain_completion_methods": "create_completion_keyword_prompt",
            "api_v1_readiness_completion_smoke_rejected_option": "temperature",
            "api_v1_readiness_yarn_requested_context_tokens": 65536,
            "api_v1_readiness_yarn_original_context_tokens": 32768,
            "api_v1_readiness_yarn_context_multiplier": 2.0,
            "api_v1_readiness_yarn_rope_freq_scale": 0.5,
            "api_v1_readiness_yarn_ext_factor_overridden": false,
            "api_v1_readiness_yarn_rope_scaling_type_source": "enum",
            "api_v1_readiness_yarn_configuration_valid": true,
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count": 28,
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special": true,
        }));
        let payload: Value = serde_json::from_str(&summary).expect("summary json");

        assert!(summary.len() <= 3500);
        assert_eq!(payload.get("type").and_then(Value::as_str), Some("error"));
        assert_eq!(
            payload.get("last_error").and_then(Value::as_str),
            Some("runtime_completion_smoke_plain_completion_worker_exception")
        );
        assert!(payload
            .get("api_v1_readiness_completion_smoke_method")
            .is_none());
        assert_eq!(
            readiness_operator_log_chunks(&serde_json::json!({
                "operator_session_id": "s1",
                "sequence": 9,
                "api_v1_readiness_completion_smoke_method": "create_completion_keyword_prompt",
                "api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg": true,
                "api_v1_readiness_yarn_requested_context_tokens": 65536,
                "api_v1_readiness_prompt_text": "unsafe",
            }))
            .into_iter()
            .map(|chunk| {
                assert!(chunk.len() <= 3500);
                serde_json::from_str::<Value>(&chunk).expect("readiness chunk json")
            })
            .filter_map(|chunk| chunk.get("diagnostics").cloned())
            .collect::<Vec<_>>()
            .len(),
            1
        );
    }

    #[test]
    fn summarize_bridge_stdout_payload_drops_unsafe_readiness_values() {
        let summary = summarize_bridge_stdout_payload(&serde_json::json!({
            "type": "error",
            "api_v1_readiness_completion_smoke_method": "create completion keyword prompt",
            "api_v1_readiness_completion_smoke_safe_summary": {"prompt": "secret"},
            "api_v1_readiness_completion_smoke_attempted_generation_kwargs": ["max_tokens"],
            "api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs": false,
            "api_v1_readiness_completion_smoke_exception_type": "LlamaCppInferenceRequestError",
            "api_v1_readiness_prompt_text": "do not log me"
        }));
        let payload: Value = serde_json::from_str(&summary).expect("summary json");

        assert!(payload
            .get("api_v1_readiness_completion_smoke_method")
            .is_none());
        assert!(payload
            .get("api_v1_readiness_completion_smoke_safe_summary")
            .is_none());
        assert!(payload
            .get("api_v1_readiness_completion_smoke_attempted_generation_kwargs")
            .is_none());
        assert!(payload.get("api_v1_readiness_prompt_text").is_none());
        // summarize_bridge_stdout_payload emits only lifecycle fields; readiness
        // diagnostic keys (even safe booleans) go to readiness_operator_log_chunks,
        // not to the stdout summary.
        assert!(payload
            .get("api_v1_readiness_completion_smoke_plain_completion_accepts_var_kwargs")
            .is_none());
        assert!(payload
            .get("api_v1_readiness_yarn_requested_context_tokens")
            .is_none());
        assert!(payload
            .get("api_v1_readiness_yarn_rope_scaling_type_source")
            .is_none());
        assert!(payload
            .get("api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count")
            .is_none());
        assert!(payload
            .get("api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special")
            .is_none());
    }

    #[test]
    fn readiness_operator_log_chunks_reconstruct_safe_sorted_diagnostics() {
        let payload = serde_json::json!({
            "type": "status",
            "operator_session_id": "s1",
            "sequence": 12,
            "api_v1_readiness_result": "failed",
            "api_v1_readiness_error_code": "compute_node_inference_failed",
            "api_v1_readiness_qwen_64k_runtime_profile_id": "qwen64k_kv_q8_fa_small_batch",
            "api_v1_readiness_qwen_64k_runtime_profile_result": "failed",
            "api_v1_readiness_qwen_64k_runtime_profile_recovery_count": 1,
            "api_v1_readiness_qwen_64k_first_readiness_failure_backend_failure_category": "backend_graph_compute_failure",
            "api_v1_readiness_completion_smoke_method": "create_completion_keyword_prompt",
            "api_v1_readiness_completion_smoke_plain_completion_backend_failure_category": "backend_graph_compute_failure",
            "api_v1_readiness_completion_smoke_plain_completion_metal_command_buffer_status": 5,
            "api_v1_readiness_completion_smoke_safe_summary": "RuntimeError:redacted",
            "api_v1_readiness_prompt_text": "unsafe prompt",
            "api_v1_readiness_completion_smoke_attempted_generation_kwargs": ["max_tokens"],
        });
        let summary = summarize_bridge_stdout_payload(&payload);
        assert!(summary.len() <= 3500);
        let summary_payload: Value = serde_json::from_str(&summary).expect("summary json");
        assert_eq!(
            summary_payload
                .get("api_v1_readiness_qwen_64k_runtime_profile_id")
                .and_then(Value::as_str),
            Some("qwen64k_kv_q8_fa_small_batch")
        );
        assert!(summary_payload
            .get("api_v1_readiness_completion_smoke_method")
            .is_none());

        let chunks = readiness_operator_log_chunks(&payload);
        assert!(!chunks.is_empty());
        let expected = safe_readiness_diagnostics_from_payload(&payload);
        let mut reconstructed = Map::new();
        for (idx, chunk) in chunks.iter().enumerate() {
            assert!(chunk.len() <= 3500);
            let event: Value = serde_json::from_str(chunk).expect("chunk json");
            assert_eq!(
                event.get("chunk_index").and_then(Value::as_u64),
                Some(idx as u64)
            );
            assert_eq!(
                event.get("chunk_count").and_then(Value::as_u64),
                Some(chunks.len() as u64)
            );
            let diagnostics = event
                .get("diagnostics")
                .and_then(Value::as_object)
                .expect("diagnostics object");
            let keys: Vec<_> = diagnostics.keys().cloned().collect();
            let mut sorted_keys = keys.clone();
            sorted_keys.sort();
            assert_eq!(keys, sorted_keys);
            for (key, value) in diagnostics {
                reconstructed.insert(key.clone(), value.clone());
            }
        }
        assert_eq!(Value::Object(reconstructed), Value::Object(expected));
    }

    #[test]
    fn operator_log_path_preserves_64k_safe_token_metadata_and_redacts_tokens() {
        let payload = serde_json::json!({
            "type": "status",
            "operator_session_id": "s64k",
            "sequence": 64,
            "running": true,
            "registered": true,
            "context_tier": "64k-full",
            "context_window_tokens": 65536,
            "api_v1_readiness_result": "passed",
            "api_v1_readiness_completion_smoke_result": "passed",
            "api_v1_readiness_yarn_requested_context_tokens": 65536,
            "api_v1_readiness_yarn_original_context_tokens": 32768,
            "api_v1_readiness_completion_smoke_plain_completion_prompt_token_count": 50,
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count": 28,
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count": 2,
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method": "llama.tokenize",
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant": "tokenize_add_bos_false_special_false",
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_ids": "tokenize_add_bos_false_special_false,tokenize_add_bos_false_special_true",
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_token_counts": "50,28",
            "token": "SECRET_STATUS_TOKEN",
            "token_ids": [1, 2, 3],
            "api_token": "SECRET_API_TOKEN",
        });

        let stdout = sanitize_operator_diagnostic_line(&summarize_bridge_stdout_payload(&payload));
        assert!(stdout.len() <= 3500);
        let stdout_event: Value = serde_json::from_str(&stdout).expect("stdout json");
        assert_eq!(stdout_event["context_window_tokens"].as_u64(), Some(65536));
        assert!(stdout_event.get("token").is_none());
        assert!(stdout_event.get("token_ids").is_none());

        let chunks = readiness_operator_log_chunks(&payload);
        assert!(!chunks.is_empty());
        let mut saw_readiness_smoke = false;
        let mut saw_requested = false;
        let mut saw_original = false;
        for chunk in chunks {
            let sanitized = sanitize_operator_diagnostic_line(&chunk);
            assert!(sanitized.len() <= 3500);
            let event: Value = serde_json::from_str(&sanitized).expect("readiness json");
            let diagnostics = event
                .get("diagnostics")
                .and_then(Value::as_object)
                .expect("diagnostics");
            if diagnostics
                .get("api_v1_readiness_completion_smoke_result")
                .and_then(Value::as_str)
                == Some("passed")
            {
                saw_readiness_smoke = true;
            }
            if diagnostics
                .get("api_v1_readiness_yarn_requested_context_tokens")
                .and_then(Value::as_u64)
                == Some(65536)
            {
                saw_requested = true;
            }
            if diagnostics
                .get("api_v1_readiness_yarn_original_context_tokens")
                .and_then(Value::as_u64)
                == Some(32768)
            {
                saw_original = true;
            }
            assert!(!sanitized.contains("SECRET_"));
        }
        assert!(saw_readiness_smoke);
        assert!(saw_requested);
        assert!(saw_original);
    }

    #[test]
    fn safe_readiness_diagnostics_rejects_free_text_strings() {
        let diagnostics = safe_readiness_diagnostics_from_payload(&serde_json::json!({
            "api_v1_readiness_completion_smoke_method": "rendered prompt leaked",
            "api_v1_readiness_completion_smoke_exception_type": "LlamaCppInferenceRequestError",
            "api_v1_readiness_yarn_requested_context_tokens": [65536],
            "api_v1_readiness_yarn_rope_scaling_type_source": "unsafe source with spaces",
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count": {"count": 28},
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special": true,
        }));

        assert!(diagnostics
            .get("api_v1_readiness_completion_smoke_method")
            .is_none());
        assert_eq!(
            diagnostics
                .get("api_v1_readiness_completion_smoke_exception_type")
                .and_then(Value::as_str),
            Some("LlamaCppInferenceRequestError")
        );
    }

    #[test]
    fn update_status_from_event_stores_only_safe_readiness_diagnostics() {
        let mut status = ComputeNodeStatus::default();
        let payload = serde_json::json!({
            "type": "error",
            "api_v1_readiness_completion_smoke_method": "create_completion_keyword_prompt",
            "api_v1_readiness_completion_smoke_safe_summary": "plain_completion_worker_exception",
            "api_v1_readiness_completion_smoke_internal_reason": "SECRET_PROMPT",
            "api_v1_readiness_completion_smoke_attempted_generation_kwargs": {"max_tokens": true},
            "api_v1_readiness_yarn_requested_context_tokens": 65536,
            "api_v1_readiness_yarn_original_context_tokens": 32768,
            "api_v1_readiness_yarn_context_multiplier": 2.0,
            "api_v1_readiness_yarn_rope_freq_scale": 0.5,
            "api_v1_readiness_yarn_ext_factor_overridden": false,
            "api_v1_readiness_yarn_rope_scaling_type_source": "enum",
            "api_v1_readiness_yarn_configuration_valid": true,
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count": 28,
            "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special": true,
        });

        assert!(update_status_from_event(&mut status, &payload));
        assert_eq!(
            status
                .readiness_diagnostics
                .get("api_v1_readiness_completion_smoke_method")
                .and_then(Value::as_str),
            Some("create_completion_keyword_prompt")
        );
        assert!(status
            .readiness_diagnostics
            .get("api_v1_readiness_completion_smoke_internal_reason")
            .is_none());
        assert!(status
            .readiness_diagnostics
            .get("api_v1_readiness_completion_smoke_attempted_generation_kwargs")
            .is_none());
        assert_eq!(
            status
                .readiness_diagnostics
                .get("api_v1_readiness_yarn_requested_context_tokens")
                .and_then(Value::as_i64),
            Some(65536)
        );
        assert_eq!(
            status
                .readiness_diagnostics
                .get("api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special")
                .and_then(Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn status_event_without_diagnostics_clears_stale_readiness_diagnostics() {
        let mut status = ComputeNodeStatus::default();
        status.readiness_diagnostics.insert(
            "api_v1_readiness_completion_smoke_method".into(),
            Value::String("create_completion_keyword_prompt".into()),
        );
        status.readiness_diagnostics.insert(
            "api_v1_readiness_yarn_requested_context_tokens".into(),
            Value::Number(65536.into()),
        );
        let payload = serde_json::json!({"type": "error", "last_error": "different_failure"});

        assert!(update_status_from_event(&mut status, &payload));
        assert!(status.readiness_diagnostics.is_empty());
    }

    #[test]
    fn started_event_clears_stale_readiness_diagnostics() {
        let mut status = ComputeNodeStatus::default();
        status.readiness_diagnostics.insert(
            "api_v1_readiness_completion_smoke_method".into(),
            Value::String("create_completion_keyword_prompt".into()),
        );
        let payload = serde_json::json!({"type": "started", "sequence": 1});

        assert!(update_status_from_event(&mut status, &payload));
        assert!(status.readiness_diagnostics.is_empty());
    }

    #[test]
    fn compute_node_event_payload_gets_current_log_file_path() {
        let payload = serde_json::json!({
            "type": "started",
            "running": true,
            "log_file_path": "/etc/passwd"
        });
        let payload = with_log_file_path(payload, Some("/tmp/operator.log"));

        assert_eq!(
            payload.get("log_file_path").and_then(Value::as_str),
            Some("/tmp/operator.log")
        );
    }

    #[test]
    fn redacts_compute_node_stderr_paths_before_operator_logging() {
        let line = sanitize_freeform_bridge_log_line(
            "desktop.runtime_setup llama_module_path=/Users/Example User/project/.venv/lib/llama_cpp/__init__.py interpreter=/Users/Example User/.venv/bin/python3 relay=https://user:pass@relay.example/path?token=secret",
        );

        assert!(line.contains("llama_module_path=<path>"));
        assert!(line.contains("interpreter=<path>"));
        assert!(line.contains("<path:python3>"));
        assert!(line.contains("relay=https://relay.example"));
        assert!(!line.contains("/Users/Example User"));
        assert!(!line.contains("token=secret"));
        assert!(!line.contains("user:pass"));
    }

    #[test]
    fn update_status_from_event_clears_log_file_path_on_null() {
        let mut status = ComputeNodeStatus {
            log_file_path: Some("/tmp/stale.log".into()),
            ..ComputeNodeStatus::default()
        };
        let payload = serde_json::json!({"type": "status", "log_file_path": null});

        assert!(update_status_from_event(&mut status, &payload));
        assert_eq!(status.log_file_path, None);
    }

    #[test]
    fn sanitize_relay_target_filters_log_control_characters() {
        assert_eq!(
            sanitize_relay_target("https://example.com\nforged=1/path?token=secret#frag"),
            "https://example.com"
        );
        assert_eq!(sanitize_relay_target("https://\n\t/path"), "unknown");
        assert_eq!(
            sanitize_relay_target("bad\nscheme://example.com"),
            "unknown"
        );
    }

    #[tokio::test]
    async fn malformed_stdout_lines_are_ignored_without_blocking_valid_events() {
        let reader = BufReader::new(
            b"{\"type\":\"status\",\"running\":true}\nnot-json\n{\"type\":\"error\",\"message\":\"boom\"}\n"
                .as_slice(),
        );
        let mut lines = reader.lines();
        let mut event_types = Vec::new();

        while let Some(line) = lines.next_line().await.expect("read line") {
            if let Ok(payload) = parse_compute_node_event_line(&line) {
                let event_type = payload
                    .get("type")
                    .and_then(Value::as_str)
                    .expect("event type");
                event_types.push(event_type.to_string());
            }
        }

        assert_eq!(event_types, vec!["status".to_string(), "error".to_string()]);
    }

    #[test]
    fn finalize_bridge_exit_preserves_warm_load_failure_state() {
        let mut status = ComputeNodeStatus {
            running: true,
            registered: false,
            relay_runtime_state: Some("failed".into()),
            warm_load_state: Some("failed".into()),
            last_error: Some("API v1 relay runtime warm-load timed out after 120s".into()),
            operator_session_id: Some("session-1".into()),
            sequence: Some(3),
            ..ComputeNodeStatus::default()
        };

        let payload = finalize_bridge_exit(
            &mut status,
            success_exit_status(),
            true,
            true,
            "session-1",
            None,
        );

        assert!(payload.is_none());
        assert!(!status.running);
        assert!(!status.registered);
        assert_eq!(status.relay_runtime_state.as_deref(), Some("failed"));
        assert_eq!(status.warm_load_state.as_deref(), Some("failed"));
        assert_eq!(
            status.last_error.as_deref(),
            Some("API v1 relay runtime warm-load timed out after 120s")
        );
    }

    #[tokio::test]
    async fn drain_compute_node_stderr_reads_all_lines() {
        let mut child = {
            #[cfg(windows)]
            {
                let mut command = Command::new("cmd");
                command.args(["/C", "echo bridge-failure 1>&2"]);
                command
            }
            #[cfg(not(windows))]
            {
                let mut command = Command::new("sh");
                command.args(["-c", "echo bridge-failure 1>&2"]);
                command
            }
        }
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn stderr script");

        let stderr = child.stderr.take().expect("stderr");
        let temp = TempDir::new().expect("tempdir");
        let log_path = temp.path().join("stderr.log");
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .expect("log file");
        let log_sink = OperatorLogSink {
            path: log_path.clone(),
            file: std::sync::Arc::new(std::sync::Mutex::new(file)),
        };
        drain_compute_node_stderr(
            stderr,
            SubprocessLogPolicy { verbose_raw: true },
            Some(log_sink),
        )
        .await
        .expect("drain stderr");
        assert!(std::fs::read_to_string(log_path)
            .expect("log")
            .contains("bridge-failure"));
        let status = child.wait().await.expect("wait child");
        assert!(status.success());
    }

    #[tokio::test]
    async fn stop_compute_node_appends_lifecycle_line_to_operator_log() {
        let temp = TempDir::new().expect("tempdir");
        let log_path = temp.path().join("operator.log");
        std::fs::write(&log_path, "").expect("create log");
        let state = ComputeNodeState::default();
        {
            let mut status = state.status.lock().await;
            status.operator_session_id = Some("session-1".into());
            status.log_file_path = Some(log_path.to_string_lossy().into_owned());
        }

        stop_compute_node(state).await.expect("stop compute node");

        let log = std::fs::read_to_string(log_path).expect("operator log");
        assert!(log.contains("desktop.compute_node.stop_requested"));
        assert!(log.contains("operator_session_id=session-1"));
    }

    #[tokio::test]
    async fn stop_compute_node_does_not_wait_on_lifecycle_lock() {
        let state = ComputeNodeState::default();
        let lifecycle_guard = state.lifecycle_lock.lock().await;

        let result =
            tokio::time::timeout(Duration::from_secs(2), stop_compute_node(state.clone())).await;

        assert!(result.is_ok(), "stop should not block on lifecycle lock");
        drop(lifecycle_guard);
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn stop_compute_node_stops_running_child_with_cancel_message() {
        let state = ComputeNodeState::default();
        let temp = TempDir::new().expect("tempdir");
        let observed_cancel_path = temp.path().join("observed-cancel.json");

        let mut child = Command::new("sh")
            .args([
                "-c",
                "IFS= read -r line; printf '%s' \"$line\" > \"$1\"; [ \"$line\" = '{\"type\":\"cancel\"}' ]",
                "sh",
                observed_cancel_path
                    .to_str()
                    .expect("cancel path should be valid UTF-8"),
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn cancel observer bridge");
        let child_stdin = child.stdin.take().expect("child stdin");

        *state.child.lock().await = Some(child);
        *state.stdin.lock().await = Some(child_stdin);
        {
            let mut status = state.status.lock().await;
            status.running = true;
            status.registered = true;
        }

        let stop_result =
            tokio::time::timeout(Duration::from_secs(2), stop_compute_node(state.clone())).await;
        assert!(stop_result.is_ok(), "stop should complete without hanging");
        stop_result
            .expect("timeout result")
            .expect("stop should succeed");

        let observed_cancel = std::fs::read_to_string(&observed_cancel_path)
            .expect("cancel message should be recorded");
        assert_eq!(observed_cancel, "{\"type\":\"cancel\"}");
        assert!(
            state.child.lock().await.is_none(),
            "child handle should be cleared"
        );
        assert!(
            state.stdin.lock().await.is_none(),
            "stdin handle should be cleared"
        );

        let final_status = state.status.lock().await.clone();
        assert!(!final_status.running);
        assert!(!final_status.registered);
    }


    #[cfg(not(windows))]
    #[tokio::test]
    async fn stop_compute_node_waits_for_bounded_bridge_cleanup() {
        let state = ComputeNodeState::default();
        let mut child = Command::new("sh")
            .args([
                "-c",
                "IFS= read -r line; [ \"$line\" = '{\"type\":\"cancel\"}' ] && sleep 2",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn slow cleanup bridge");
        let child_stdin = child.stdin.take().expect("child stdin");

        *state.child.lock().await = Some(child);
        *state.stdin.lock().await = Some(child_stdin);
        {
            let mut status = state.status.lock().await;
            status.running = true;
            status.registered = true;
        }

        let started = Instant::now();
        let stop_result =
            tokio::time::timeout(Duration::from_secs(5), stop_compute_node(state.clone())).await;
        assert!(stop_result.is_ok(), "stop should allow slow cleanup without timing out");
        stop_result
            .expect("timeout result")
            .expect("stop should succeed");
        assert!(
            started.elapsed() >= Duration::from_millis(1500),
            "stop should wait for the bridge cleanup outcome instead of killing after one second"
        );

        let final_status = state.status.lock().await.clone();
        assert!(!final_status.running);
        assert!(!final_status.registered);
    }

    #[cfg(not(windows))]
    #[tokio::test]
    async fn repeated_start_stop_start_does_not_leave_stale_child_handle() {
        let state = ComputeNodeState::default();
        let mut first_child = Command::new("sh")
            .args(["-c", "IFS= read -r _line; exit 0"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn first bridge");
        let first_stdin = first_child.stdin.take().expect("first stdin");
        *state.child.lock().await = Some(first_child);
        *state.stdin.lock().await = Some(first_stdin);

        stop_compute_node(state.clone()).await.expect("first stop");
        assert!(state.child.lock().await.is_none());
        assert!(state.stdin.lock().await.is_none());

        let mut second_child = Command::new("sh")
            .args(["-c", "IFS= read -r _line; exit 0"])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn second bridge");
        let second_stdin = second_child.stdin.take().expect("second stdin");
        *state.child.lock().await = Some(second_child);
        *state.stdin.lock().await = Some(second_stdin);

        stop_compute_node(state.clone()).await.expect("second stop");
        assert!(state.child.lock().await.is_none());
        assert!(state.stdin.lock().await.is_none());
    }

    #[test]
    fn update_status_from_event_preserves_relay_runtime_readiness_fields() {
        let mut status = ComputeNodeStatus {
            running: true,
            registered: true,
            warm_load_state: Some("ready".into()),
            warm_load_enabled: Some(true),
            warm_load_duration_ms: Some(125),
            runtime_path: Some("bridge".into()),
            relay_runtime_path: Some("bridge".into()),
            ..ComputeNodeStatus::default()
        };

        let payload = serde_json::json!({
            "type": "status",
            "registered": true,
            "warm_load_state": "warming",
            "warm_load_enabled": true,
            "warm_load_duration_ms": 250,
            "runtime_path": "sidecar",
            "relay_runtime_path": "bridge"
        });

        update_status_from_event(&mut status, &payload);

        assert!(status.registered);
        assert_eq!(status.warm_load_state.as_deref(), Some("warming"));
        assert_eq!(status.warm_load_enabled, Some(true));
        assert_eq!(status.warm_load_duration_ms, Some(250));
        assert_eq!(status.runtime_path.as_deref(), Some("sidecar"));
        assert_eq!(status.relay_runtime_path.as_deref(), Some("bridge"));
    }

    #[test]
    fn update_status_from_event_ignores_stale_prior_session_events() {
        let mut status = ComputeNodeStatus {
            running: true,
            registered: false,
            relay_runtime_state: Some("warming".into()),
            operator_session_id: Some("new-session".into()),
            sequence: Some(4),
            ..ComputeNodeStatus::default()
        };

        let old_session = serde_json::json!({
            "type": "status",
            "running": false,
            "registered": false,
            "relay_runtime_state": "stopped",
            "last_error": "old process failed after restart",
            "operator_session_id": "old-session",
            "sequence": 99
        });
        let old_sequence = serde_json::json!({
            "type": "status",
            "running": false,
            "registered": false,
            "relay_runtime_state": "stopped",
            "last_error": "older event failed after restart",
            "operator_session_id": "new-session",
            "sequence": 3
        });

        assert!(!update_status_from_event(&mut status, &old_session));
        assert!(!update_status_from_event(&mut status, &old_sequence));
        assert!(status.running);
        assert_eq!(status.relay_runtime_state.as_deref(), Some("warming"));
        assert!(status.last_error.is_none());
    }

    #[test]
    fn update_status_from_event_rejects_duplicate_sequences_and_cross_session_starts() {
        let mut running_status = ComputeNodeStatus {
            running: true,
            registered: false,
            relay_runtime_state: Some("starting".into()),
            operator_session_id: Some("new-session".into()),
            sequence: Some(1),
            ..ComputeNodeStatus::default()
        };

        let duplicate_sequence = serde_json::json!({
            "type": "status",
            "running": false,
            "registered": false,
            "relay_runtime_state": "stopped",
            "operator_session_id": "new-session",
            "sequence": 1
        });
        let old_started_event = serde_json::json!({
            "type": "started",
            "running": true,
            "registered": false,
            "relay_runtime_state": "ready",
            "operator_session_id": "old-session",
            "sequence": 1
        });

        assert!(!update_status_from_event(
            &mut running_status,
            &duplicate_sequence
        ));
        assert!(!update_status_from_event(
            &mut running_status,
            &old_started_event
        ));
        assert_eq!(
            running_status.operator_session_id.as_deref(),
            Some("new-session")
        );
        assert_eq!(
            running_status.relay_runtime_state.as_deref(),
            Some("starting")
        );

        let mut stopped_status = ComputeNodeStatus {
            running: false,
            registered: false,
            relay_runtime_state: Some("stopped".into()),
            operator_session_id: Some("old-session".into()),
            sequence: Some(8),
            ..ComputeNodeStatus::default()
        };
        let new_started_event = serde_json::json!({
            "type": "started",
            "running": true,
            "registered": false,
            "relay_runtime_state": "starting",
            "operator_session_id": "new-session",
            "sequence": 1
        });

        assert!(update_status_from_event(
            &mut stopped_status,
            &new_started_event
        ));
        assert!(stopped_status.running);
        assert_eq!(
            stopped_status.operator_session_id.as_deref(),
            Some("new-session")
        );
    }

    #[test]
    fn update_status_from_event_rejects_stale_worker_generation_without_overwriting_status() {
        let mut status = ComputeNodeStatus {
            running: true,
            registered: true,
            relay_runtime_state: Some("ready".into()),
            worker_state: Some("ready".into()),
            worker_generation: Some(5),
            worker_restart_count: Some(2),
            worker_alive: Some(true),
            last_worker_error_code: None,
            operator_session_id: Some("session-1".into()),
            sequence: Some(10),
            ..ComputeNodeStatus::default()
        };

        let stale_worker_event = serde_json::json!({
            "type": "status",
            "running": false,
            "registered": false,
            "relay_runtime_state": "failed",
            "last_error": "stale event should not overwrite non-worker status",
            "worker_state": "failed",
            "worker_generation": 4,
            "worker_restart_count": 99,
            "worker_alive": false,
            "last_worker_error_code": "stale_worker_failure",
            "operator_session_id": "session-1",
            "sequence": 11
        });

        assert!(!update_status_from_event(&mut status, &stale_worker_event));
        assert!(status.running);
        assert!(status.registered);
        assert_eq!(status.relay_runtime_state.as_deref(), Some("ready"));
        assert!(status.last_error.is_none());
        assert_eq!(status.worker_state.as_deref(), Some("ready"));
        assert_eq!(status.worker_generation, Some(5));
        assert_eq!(status.worker_restart_count, Some(2));
        assert_eq!(status.worker_alive, Some(true));
        assert!(status.last_worker_error_code.is_none());
    }

    #[test]
    fn update_status_from_event_preserves_generation_on_null_payload_for_stale_guard() {
        let mut status = ComputeNodeStatus {
            running: true,
            registered: true,
            relay_runtime_state: Some("ready".into()),
            worker_state: Some("ready".into()),
            worker_generation: Some(5),
            worker_restart_count: Some(2),
            worker_alive: Some(true),
            last_worker_error_code: Some("previous_worker_failure".into()),
            operator_session_id: Some("session-1".into()),
            sequence: Some(10),
            ..ComputeNodeStatus::default()
        };

        let fallback_status_event = serde_json::json!({
            "type": "status",
            "running": true,
            "registered": true,
            "relay_runtime_state": "ready",
            "worker_generation": null,
            "last_worker_error_code": null,
            "operator_session_id": "session-1",
            "sequence": 11
        });

        assert!(update_status_from_event(
            &mut status,
            &fallback_status_event
        ));
        assert_eq!(status.worker_generation, Some(5));
        assert!(status.last_worker_error_code.is_none());

        let stale_worker_event = serde_json::json!({
            "type": "status",
            "running": false,
            "registered": false,
            "relay_runtime_state": "failed",
            "worker_state": "failed",
            "worker_generation": 4,
            "worker_restart_count": 99,
            "worker_alive": false,
            "last_worker_error_code": "stale_worker_failure",
            "operator_session_id": "session-1",
            "sequence": 12
        });

        assert!(!update_status_from_event(&mut status, &stale_worker_event));
        assert!(status.running);
        assert!(status.registered);
        assert_eq!(status.relay_runtime_state.as_deref(), Some("ready"));
        assert_eq!(status.worker_state.as_deref(), Some("ready"));
        assert_eq!(status.worker_generation, Some(5));
        assert_eq!(status.worker_restart_count, Some(2));
        assert_eq!(status.worker_alive, Some(true));
        assert!(status.last_worker_error_code.is_none());
    }

    #[test]
    fn update_status_from_event_allows_fresh_new_session_to_reset_worker_generation() {
        let mut status = ComputeNodeStatus {
            running: false,
            registered: false,
            relay_runtime_state: Some("stopped".into()),
            worker_state: Some("failed".into()),
            worker_generation: Some(9),
            worker_restart_count: Some(4),
            worker_alive: Some(false),
            last_worker_error_code: Some("old_failure".into()),
            operator_session_id: Some("old-session".into()),
            sequence: Some(20),
            ..ComputeNodeStatus::default()
        };

        let fresh_started_event = serde_json::json!({
            "type": "started",
            "running": true,
            "registered": false,
            "relay_runtime_state": "starting",
            "worker_state": "starting",
            "worker_generation": 1,
            "worker_restart_count": 0,
            "worker_alive": false,
            "last_worker_error_code": null,
            "operator_session_id": "new-session",
            "sequence": 1
        });

        assert!(update_status_from_event(&mut status, &fresh_started_event));
        assert!(status.running);
        assert_eq!(status.operator_session_id.as_deref(), Some("new-session"));
        assert_eq!(status.sequence, Some(1));
        assert_eq!(status.relay_runtime_state.as_deref(), Some("starting"));
        assert_eq!(status.worker_state.as_deref(), Some("starting"));
        assert_eq!(status.worker_generation, Some(1));
        assert_eq!(status.worker_restart_count, Some(0));
        assert_eq!(status.worker_alive, Some(false));
        assert!(status.last_worker_error_code.is_none());
    }

    #[test]
    fn compute_node_status_cache_replays_warming_relay_runtime_fields() {
        let state = ComputeNodeState::default();
        let cached_status = ComputeNodeStatus {
            running: true,
            registered: false,
            warm_load_state: Some("warming".into()),
            warm_load_enabled: Some(true),
            warm_load_duration_ms: Some(42),
            runtime_path: Some("sidecar".into()),
            relay_runtime_path: Some("bridge".into()),
            ..ComputeNodeStatus::default()
        };

        let mut status = state.status.blocking_lock();
        *status = cached_status.clone();

        assert_eq!(status.warm_load_state, cached_status.warm_load_state);
        assert_eq!(status.warm_load_enabled, cached_status.warm_load_enabled);
        assert_eq!(
            status.warm_load_duration_ms,
            cached_status.warm_load_duration_ms
        );
        assert_eq!(status.runtime_path, cached_status.runtime_path);
        assert_eq!(status.relay_runtime_path, cached_status.relay_runtime_path);
    }

    #[test]
    fn bridge_session_env_vars_include_operator_log_path_when_available() {
        let env_vars = bridge_session_env_vars("session-1", Some("/tmp/operator.log"));

        assert!(env_vars.contains(&(
            "TOKENPLACE_COMPUTE_NODE_SESSION_ID",
            "session-1".to_string()
        )));
        assert!(env_vars.contains(&(
            "TOKENPLACE_OPERATOR_LOG_FILE",
            "/tmp/operator.log".to_string()
        )));
    }

    #[test]
    fn bridge_session_env_vars_omit_operator_log_path_when_unavailable() {
        let env_vars = bridge_session_env_vars("session-1", None);

        assert_eq!(env_vars.len(), 1);
        assert_eq!(
            env_vars[0],
            (
                "TOKENPLACE_COMPUTE_NODE_SESSION_ID",
                "session-1".to_string()
            )
        );
    }

    #[test]
    fn startup_failure_status_records_resolver_error_and_not_running() {
        let request = ComputeNodeRequest {
            model_path: "model.gguf".into(),
            relay_base_url: "https://relay.example".into(),
            relay_base_urls: vec![],
            mode: ComputeMode::Cpu,
            context_tier: "64k-full".into(),
        };
        let status = startup_failure_status(
            &request,
            "no usable Python 3 interpreter found for desktop Python subprocess".into(),
            Some("session-1".into()),
            Some("/tmp/operator.log".into()),
        );

        assert!(!status.running);
        assert!(!status.registered);
        assert_eq!(
            status.last_error.as_deref(),
            Some("no usable Python 3 interpreter found for desktop Python subprocess")
        );
        assert_eq!(status.active_relay_url, request.relay_base_url);
        assert_eq!(status.model_path, request.model_path);
    }

    #[test]
    fn bridge_exit_error_reports_missing_startup_event_even_on_clean_exit() {
        let exit_status = success_exit_status();
        assert!(exit_status.success());

        let last_error = bridge_exit_error(exit_status, false, None);
        assert!(last_error.is_some());
        assert!(last_error
            .as_deref()
            .is_some_and(|message| message.contains("before emitting a startup event")));
    }

    #[test]
    fn bridge_exit_error_is_none_after_started_event_and_clean_exit() {
        let exit_status = success_exit_status();
        assert!(exit_status.success());

        assert!(bridge_exit_error(exit_status, true, None).is_none());
    }

    #[test]
    fn finalize_bridge_exit_preserves_safe_readiness_diagnostics_in_error_payload() {
        let exit_status = {
            #[cfg(windows)]
            {
                StdCommand::new("cmd")
                    .args(["/C", "exit", "2"])
                    .status()
                    .expect("status")
            }
            #[cfg(not(windows))]
            {
                StdCommand::new("sh")
                    .args(["-c", "exit 2"])
                    .status()
                    .expect("status")
            }
        };
        let mut status = ComputeNodeStatus {
            operator_session_id: Some("session-1".into()),
            running: true,
            sequence: Some(3),
            ..Default::default()
        };
        status.readiness_diagnostics.insert(
            "api_v1_readiness_completion_smoke_method".into(),
            Value::String("create_completion_keyword_prompt".into()),
        );
        status.readiness_diagnostics.insert(
            "api_v1_readiness_yarn_requested_context_tokens".into(),
            Value::Number(65536.into()),
        );

        let payload =
            finalize_bridge_exit(&mut status, exit_status, true, false, "session-1", None)
                .expect("exit payload");

        assert_eq!(
            payload
                .get("readiness_diagnostics")
                .and_then(Value::as_object)
                .and_then(|diagnostics| diagnostics.get("api_v1_readiness_completion_smoke_method"))
                .and_then(Value::as_str),
            Some("create_completion_keyword_prompt")
        );
        assert_eq!(
            payload
                .get("readiness_diagnostics")
                .and_then(Value::as_object)
                .and_then(
                    |diagnostics| diagnostics.get("api_v1_readiness_yarn_requested_context_tokens")
                )
                .and_then(Value::as_i64),
            Some(65536)
        );
    }

    #[test]
    fn bridge_exit_error_includes_recent_stderr_diagnostic_tail() {
        let exit_status = success_exit_status();
        let tail = "123 desktop.compute_node.stderr first line\n124 desktop.compute_node.stdout mentions desktop.compute_node.stderr but is ignored\n125 desktop.compute_node.stderr ModuleNotFoundError: No module named 'utils.context_profiles'\n";

        let last_error = bridge_exit_error(exit_status, false, Some(tail))
            .expect("missing startup event should be an error");

        assert!(last_error.contains("before emitting a startup event"));
        assert!(last_error.contains("recent diagnostic:"));
        assert!(last_error.contains("desktop.compute_node.stderr ModuleNotFoundError"));
        assert!(last_error.contains("utils.context_profiles"));
        assert!(!last_error.contains("but is ignored"));
    }

    #[test]
    fn finalize_bridge_exit_uses_fallback_log_path_before_startup_event() {
        let temp = TempDir::new().expect("tempdir");
        let log_path = temp.path().join("operator.log");
        std::fs::write(
            &log_path,
            "123 desktop.compute_node.stderr ModuleNotFoundError: No module named 'utils.context_profiles'\n",
        )
        .expect("write operator log");
        let mut status = ComputeNodeStatus {
            running: true,
            registered: true,
            operator_session_id: Some("current-session".into()),
            sequence: Some(7),
            log_file_path: None,
            ..ComputeNodeStatus::default()
        };
        let exit_status = success_exit_status();

        finalize_bridge_exit(
            &mut status,
            exit_status,
            false,
            false,
            "current-session",
            Some(log_path.to_string_lossy().as_ref()),
        )
        .expect("error payload should be emitted");

        let last_error = status.last_error.as_deref().expect("last error");
        assert!(last_error.contains("recent diagnostic:"));
        assert!(last_error.contains("utils.context_profiles"));
    }

    #[test]
    fn finalize_bridge_exit_emits_ui_error_payload_when_clean_exit_happens_before_startup_event() {
        let mut status = ComputeNodeStatus {
            running: true,
            registered: true,
            operator_session_id: Some("current-session".into()),
            sequence: Some(7),
            ..ComputeNodeStatus::default()
        };
        let exit_status = success_exit_status();

        let payload = finalize_bridge_exit(
            &mut status,
            exit_status,
            false,
            false,
            "current-session",
            None,
        )
        .expect("error payload should be emitted");

        assert!(!status.running);
        assert!(!status.registered);
        let last_error = status
            .last_error
            .as_deref()
            .expect("status last_error should be set");
        assert!(last_error.contains("before emitting a startup event"));
        assert_eq!(payload.get("type").and_then(Value::as_str), Some("error"));
        assert_eq!(payload.get("running").and_then(Value::as_bool), Some(false));
        assert_eq!(
            payload.get("registered").and_then(Value::as_bool),
            Some(false)
        );
        assert_eq!(
            payload.get("last_error").and_then(Value::as_str),
            Some(last_error)
        );
        assert_eq!(
            payload.get("operator_session_id").and_then(Value::as_str),
            Some("current-session")
        );
        assert_eq!(payload.get("sequence").and_then(Value::as_u64), Some(8));
        assert!(payload
            .get("updated_at_ms")
            .and_then(Value::as_u64)
            .is_some());
        assert_eq!(status.sequence, Some(8));
    }

    #[test]
    fn finalize_bridge_exit_suppresses_payload_for_superseded_session() {
        let mut status = ComputeNodeStatus {
            running: true,
            registered: true,
            operator_session_id: Some("new-session".into()),
            sequence: Some(3),
            relay_runtime_state: Some("starting".into()),
            ..ComputeNodeStatus::default()
        };
        let exit_status = success_exit_status();

        let payload =
            finalize_bridge_exit(&mut status, exit_status, false, false, "old-session", None);

        assert!(payload.is_none());
        assert!(status.running);
        assert!(status.registered);
        assert_eq!(status.sequence, Some(3));
        assert_eq!(status.relay_runtime_state.as_deref(), Some("starting"));
    }

    #[test]
    fn versioned_bridge_exit_error_cannot_be_accepted_after_restart() {
        let mut exiting_status = ComputeNodeStatus {
            running: true,
            registered: true,
            operator_session_id: Some("old-session".into()),
            sequence: Some(2),
            ..ComputeNodeStatus::default()
        };
        let exit_status = success_exit_status();
        let payload = finalize_bridge_exit(
            &mut exiting_status,
            exit_status,
            false,
            false,
            "old-session",
            None,
        )
        .expect("old session should emit a versioned synthetic error");
        assert_eq!(
            payload.get("operator_session_id").and_then(Value::as_str),
            Some("old-session")
        );
        assert_eq!(payload.get("sequence").and_then(Value::as_u64), Some(3));
        assert!(payload
            .get("updated_at_ms")
            .and_then(Value::as_u64)
            .is_some());

        let mut restarted_status = ComputeNodeStatus {
            running: true,
            registered: true,
            relay_runtime_state: Some("ready".into()),
            operator_session_id: Some("new-session".into()),
            sequence: Some(1),
            ..ComputeNodeStatus::default()
        };

        assert!(!update_status_from_event(&mut restarted_status, &payload));
        assert!(restarted_status.running);
        assert!(restarted_status.registered);
        assert_eq!(
            restarted_status.operator_session_id.as_deref(),
            Some("new-session")
        );
        assert!(restarted_status.last_error.is_none());
    }

    #[test]
    fn compute_bridge_missing_macos_app_resources_reports_attempts_without_dev_fallback() {
        let temp = TempDir::new().expect("tempdir");
        let app_root = temp.path().join("TokenPlace.app");
        let exe_path = app_root.join("Contents").join("MacOS").join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");

        let error = resolve_bridge_script_for(Some(&exe_path), &manifest_dir, None, None)
            .expect_err("missing compute bridge should fail closed");

        assert!(error.contains("compute_node_bridge.py"));
        assert!(error.contains("attempted_resource_roots="));
        assert!(error.contains("attempted_bridge_paths="));
        assert!(error.contains("MacOsAppResources"));
        assert!(error.contains("Contents/Resources/python/compute_node_bridge.py"));
        assert!(error.contains("interpreter=<unresolved>"));
    }

    #[test]
    fn startup_failure_status_clears_visible_running_session_state_for_resolution_errors() {
        let request = ComputeNodeRequest {
            model_path: "model.gguf".into(),
            relay_base_url: "https://relay.example".into(),
            relay_base_urls: vec![],
            mode: ComputeMode::Auto,
            context_tier: "unknown".into(),
        };

        let status = startup_failure_status(
            &request,
            "unable to locate desktop Python bridge script 'compute_node_bridge.py'".into(),
            None,
            None,
        );

        assert!(!status.running);
        assert!(!status.registered);
        assert_eq!(status.operator_session_id, None);
        assert_eq!(status.sequence, None);
        assert_eq!(status.relay_runtime_state.as_deref(), Some("failed"));
        assert_eq!(status.warm_load_state.as_deref(), Some("failed"));
        assert!(status
            .last_error
            .as_deref()
            .unwrap_or_default()
            .contains("compute_node_bridge.py"));
    }

    #[test]
    fn bridge_script_candidates_include_packaged_resource_locations() {
        let temp = TempDir::new().expect("tempdir");
        let app_root = temp.path().join("Token Place.app");
        let exe_dir = app_root.join("Contents").join("MacOS");
        let exe_path = exe_dir.join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");
        let candidates = bridge_script_candidates(Some(&exe_path), &manifest_dir, None);

        assert!(candidates
            .iter()
            .any(|candidate| candidate.ends_with("resources/python/compute_node_bridge.py")));
        assert!(candidates
            .iter()
            .any(|candidate| candidate.ends_with("Resources/python/compute_node_bridge.py")));
        assert_eq!(
            candidates.last().expect("manifest candidate"),
            &manifest_dir.join("python").join("compute_node_bridge.py")
        );
    }

    #[test]
    fn first_existing_script_finds_macos_app_resources_bridge_path() {
        let temp = TempDir::new().expect("tempdir");
        let app_root = temp.path().join("TokenPlace.app");
        let exe_dir = app_root.join("Contents").join("MacOS");
        let resources_dir = app_root.join("Contents").join("Resources").join("python");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");
        let bridge = resources_dir.join("compute_node_bridge.py");
        std::fs::write(&bridge, "print('ok')\n").expect("write bridge");

        let exe_path = exe_dir.join("token.place");
        let candidates = bridge_script_candidates(Some(&exe_path), temp.path(), None);
        let resolved = first_existing_script(candidates).expect("resolved bridge path");

        assert_eq!(Path::new(&resolved), bridge);
    }

    #[test]
    fn first_existing_script_finds_packaged_resource_bridge_path() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");
        let bridge = resources_dir.join("compute_node_bridge.py");
        std::fs::write(&bridge, "print('ok')\n").expect("write bridge");

        let exe_path = exe_dir.join("token.place.exe");
        let candidates = bridge_script_candidates(Some(&exe_path), temp.path(), None);
        let resolved = first_existing_script(candidates).expect("resolved bridge path");

        assert_eq!(Path::new(&resolved), bridge);
    }

    #[test]
    fn first_existing_script_prefers_resources_over_exe_python_bridge_path() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let exe_python_dir = exe_dir.join("python");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&exe_python_dir).expect("create exe python dir");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");

        let exe_bridge = exe_python_dir.join("compute_node_bridge.py");
        std::fs::write(&exe_bridge, "print('exe')\n").expect("write exe bridge");
        let resources_bridge = resources_dir.join("compute_node_bridge.py");
        std::fs::write(&resources_bridge, "print('resources')\n").expect("write resources bridge");

        let exe_path = exe_dir.join("token.place");
        let candidates = bridge_script_candidates(Some(&exe_path), temp.path(), None);
        let resolved = first_existing_script(candidates).expect("resolved bridge path");

        assert_eq!(Path::new(&resolved), resources_bridge);
    }

    #[test]
    fn bridge_script_candidates_include_runtime_resource_and_windows_updater_paths() {
        let temp = TempDir::new().expect("tempdir");
        let resource_dir = temp.path().join("runtime-resources");
        let exe_dir = temp.path().join("Local").join("token.place");
        let exe_path = exe_dir.join("token.place.exe");
        let candidates =
            bridge_script_candidates(Some(&exe_path), temp.path(), Some(&resource_dir));

        assert_eq!(
            candidates.first().expect("first candidate"),
            &resource_dir.join("python").join("compute_node_bridge.py")
        );
        assert!(candidates.iter().any(|candidate| {
            candidate.ends_with("_up_/resources/python/compute_node_bridge.py")
        }));
    }

    #[test]
    fn configure_runtime_bootstrap_env_sets_enable_flag_for_gpu_mode() {
        let mut command = Command::new("python");
        configure_runtime_bootstrap_env(&mut command, &ComputeMode::Hybrid);

        let expected = if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
            Some("1")
        } else {
            None
        };
        assert_eq!(
            command_env_value(&command, ENABLE_RUNTIME_BOOTSTRAP_ENV).as_deref(),
            expected
        );
    }

    #[test]
    fn configure_runtime_bootstrap_env_omits_enable_flag_for_cpu_mode_and_when_disabled() {
        let mut cpu_command = Command::new("python");
        configure_runtime_bootstrap_env(&mut cpu_command, &ComputeMode::Cpu);
        assert_eq!(
            command_env_value(&cpu_command, ENABLE_RUNTIME_BOOTSTRAP_ENV),
            None
        );

        let disable_key = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP";
        let previous = std::env::var(disable_key).ok();
        // SAFETY: This unit test mutates process env in a tightly scoped block and restores it.
        unsafe {
            std::env::set_var(disable_key, "1");
        }
        let mut disabled_command = Command::new("python");
        configure_runtime_bootstrap_env(&mut disabled_command, &ComputeMode::Gpu);
        if let Some(value) = previous {
            // SAFETY: restore prior process env for test isolation.
            unsafe {
                std::env::set_var(disable_key, value);
            }
        } else {
            // SAFETY: restore prior process env for test isolation.
            unsafe {
                std::env::remove_var(disable_key);
            }
        }

        assert_eq!(
            command_env_value(&disabled_command, ENABLE_RUNTIME_BOOTSTRAP_ENV),
            None
        );
    }
}
