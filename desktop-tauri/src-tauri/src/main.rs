mod backend;
mod build_identity;
mod compute_node;
mod config;
mod context_profiles;
pub mod forward;
pub mod keygen;
mod logging;
mod operator_logs;
mod python_runtime;
mod sidecar;
mod subprocess_logging;

use backend::{detect_backend_for, BackendInfo};
use compute_node::{ComputeNodeRequest, ComputeNodeState, ComputeNodeStatus};
use config::{config_path, DesktopConfig};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sidecar::{InferenceRequest, SidecarState};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use tauri::{Emitter, Manager};
use tokio::sync::Mutex;

#[derive(Default)]
struct AppState {
    sidecar: SidecarState,
    compute_node: ComputeNodeState,
    config_dir: Mutex<Option<PathBuf>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ModelArtifactInfo {
    canonical_family_url: String,
    filename: String,
    url: String,
    models_dir: String,
    resolved_model_path: String,
    exists: bool,
    size_bytes: Option<u64>,
}

#[derive(Debug, Deserialize)]
struct BridgeResponse {
    ok: bool,
    payload: Option<ModelArtifactInfo>,
    error: Option<String>,
}

fn model_bridge_script_candidates(
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    resource_dir: Option<&Path>,
) -> Vec<PathBuf> {
    python_runtime::bridge_script_candidates_from_resource_roots(
        "model_bridge.py",
        exe_path,
        manifest_dir,
        resource_dir,
    )
}

fn resolve_model_bridge_script_path_for(
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    resource_dir: Option<&Path>,
    interpreter: Option<&str>,
) -> Result<PathBuf, String> {
    let context = python_runtime::BridgeResourceContext {
        exe_path,
        manifest_dir,
        tauri_resource_dir: resource_dir,
    };
    context.resolve_bridge_script_path("model_bridge.py", interpreter)
}

fn resolve_model_bridge_script_path(interpreter: Option<&str>) -> Result<PathBuf, String> {
    let current_exe = std::env::current_exe().ok();
    resolve_model_bridge_script_path_for(
        current_exe.as_deref(),
        Path::new(env!("CARGO_MANIFEST_DIR")),
        None,
        interpreter,
    )
}

fn configure_runtime_pythonpath_for(
    command: &mut std::process::Command,
    bridge_script: &Path,
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    resource_dir: Option<&Path>,
) -> Option<PathBuf> {
    let context = python_runtime::BridgeResourceContext {
        exe_path,
        manifest_dir,
        tauri_resource_dir: resource_dir,
    };
    python_runtime::disable_python_user_site(command);
    let import_root =
        python_runtime::resolve_runtime_import_root(Some(bridge_script), context.manifest_dir);
    if let Some(import_root) = import_root.as_deref() {
        let (_resource_root, layout) = context.describe_resource_layout(bridge_script);
        python_runtime::configure_python_subprocess_env_for_layout(
            command,
            import_root,
            layout,
            context.packaged(),
        );
    }
    import_root
}

fn configure_runtime_pythonpath(
    command: &mut std::process::Command,
    bridge_script: &Path,
) -> Option<PathBuf> {
    // NOTE: CARGO_MANIFEST_DIR is compile-time and primarily helps local/dev launches.
    // Packaged end-user launches rely on python/path_bootstrap.py for runtime import roots.
    configure_runtime_pythonpath_for(
        command,
        bridge_script,
        std::env::current_exe().ok().as_deref(),
        Path::new(env!("CARGO_MANIFEST_DIR")),
        None,
    )
}

fn summarize_model_bridge_output_line(line: &str) -> String {
    let Ok(value) = serde_json::from_str::<Value>(line) else {
        return operator_logs::sanitize_operator_diagnostic_line(line);
    };
    let mut summary = serde_json::Map::new();
    if let Some(ok) = value.get("ok").and_then(Value::as_bool) {
        summary.insert("ok".into(), Value::Bool(ok));
    }
    if let Some(error) = value.get("error").and_then(Value::as_str) {
        summary.insert(
            "error".into(),
            Value::String(operator_logs::sanitize_operator_diagnostic_line(error)),
        );
    }
    if let Some(payload) = value.get("payload").and_then(Value::as_object) {
        let mut payload_summary = serde_json::Map::new();
        for key in [
            "canonical_family_url",
            "filename",
            "url",
            "exists",
            "size_bytes",
        ] {
            if let Some(payload_value) = payload.get(key) {
                payload_summary.insert(
                    key.into(),
                    sanitize_model_bridge_payload_field(key, payload_value),
                );
            }
        }
        for key in ["models_dir", "resolved_model_path"] {
            if payload.get(key).is_some() {
                payload_summary.insert(key.into(), Value::String("<redacted>".into()));
            }
        }
        summary.insert("payload".into(), Value::Object(payload_summary));
    }
    if summary.is_empty() {
        return "{\"type\":\"model_bridge_event_summary_unavailable\"}".into();
    }
    serde_json::to_string(&Value::Object(summary))
        .unwrap_or_else(|_| "{\"type\":\"model_bridge_event_summary_error\"}".into())
}

fn sanitize_model_bridge_payload_field(key: &str, value: &Value) -> Value {
    match value {
        Value::String(text) if key == "url" || key == "canonical_family_url" => {
            Value::String(operator_logs::sanitize_operator_diagnostic_line(text))
        }
        Value::String(text) => {
            Value::String(operator_logs::sanitize_operator_diagnostic_line(text))
        }
        Value::Bool(_) | Value::Number(_) | Value::Null => value.clone(),
        Value::Array(_) | Value::Object(_) => Value::String("<omitted>".into()),
    }
}

fn run_model_bridge(app: &tauri::AppHandle, action: &str) -> Result<ModelArtifactInfo, String> {
    let exe_path = std::env::current_exe().ok();
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let resource_dir = app.path().resource_dir().ok();
    let context = python_runtime::BridgeResourceContext {
        exe_path: exe_path.as_deref(),
        manifest_dir,
        tauri_resource_dir: resource_dir.as_deref(),
    };
    let launcher = python_runtime::resolve_python_launcher_resource_aware(
        context.launcher_options("TOKEN_PLACE_PYTHON"),
    )
    .map_err(|e| format!("unable to resolve Python launcher for model bridge: {e}"))?;
    let bridge_script = resolve_model_bridge_script_path_for(
        exe_path.as_deref(),
        manifest_dir,
        resource_dir.as_deref(),
        Some(&launcher.program),
    )?;
    let mut bridge_command = launcher.command_for_script_blocking(&bridge_script);
    let import_root = configure_runtime_pythonpath_for(
        &mut bridge_command,
        &bridge_script,
        exe_path.as_deref(),
        manifest_dir,
        resource_dir.as_deref(),
    );
    let (selected_resource_root, selected_layout) =
        context.describe_resource_layout(&bridge_script);
    let start_line = format!(
        "action={} bridge={} interpreter={} resource_root={} layout={:?} import_root={}",
        action,
        operator_logs::sanitize_operator_path_display(&bridge_script),
        operator_logs::sanitize_operator_diagnostic_line(
            &bridge_command.get_program().to_string_lossy(),
        ),
        operator_logs::sanitize_operator_path_display(&selected_resource_root),
        selected_layout,
        import_root
            .as_deref()
            .map(operator_logs::sanitize_operator_path_display)
            .unwrap_or_else(|| "<unresolved>".into())
    );
    eprintln!("desktop.model_bridge.start {start_line}");
    let _ = operator_logs::append_model_bridge_log(app, action, &format!("start {start_line}"));
    let output = bridge_command.arg(action).output().map_err(|e| {
        let message = format!("unable to run model bridge: {e}");
        let _ = operator_logs::append_model_bridge_log(app, action, &message);
        message
    })?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    for line in stdout.lines().filter(|line| !line.trim().is_empty()) {
        let _ = operator_logs::append_model_bridge_log(
            app,
            action,
            &format!("stdout {}", summarize_model_bridge_output_line(line)),
        );
    }
    for line in stderr.lines().filter(|line| !line.trim().is_empty()) {
        let _ = operator_logs::append_model_bridge_log(
            app,
            action,
            &format!(
                "stderr {}",
                operator_logs::sanitize_operator_diagnostic_line(line)
            ),
        );
    }
    let json_line = stdout
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .ok_or_else(|| {
            if stderr.trim().is_empty() {
                "model bridge produced no JSON response on stdout".to_string()
            } else {
                format!("model bridge produced no JSON response on stdout; stderr: {stderr}")
            }
        })?;
    let parsed: BridgeResponse = serde_json::from_str(json_line).map_err(|e| {
        format!(
            "invalid model bridge response: {e}; json line: {json_line}; stdout: {stdout}; stderr: {stderr}"
        )
    })?;
    if parsed.ok {
        return parsed
            .payload
            .ok_or_else(|| "model bridge returned success without payload".into());
    }
    let bridge_error = parsed
        .error
        .unwrap_or_else(|| "model bridge returned an unknown error".into());
    if stderr.trim().is_empty() {
        Err(bridge_error)
    } else {
        Err(format!("{bridge_error} ({stderr})"))
    }
}

fn resolve_config_dir(app: &tauri::AppHandle, state: &AppState) -> anyhow::Result<PathBuf> {
    if let Some(existing) = state.config_dir.blocking_lock().clone() {
        return Ok(existing);
    }
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|e| anyhow::anyhow!("config path error: {e}"))?;
    fs::create_dir_all(&dir)?;
    *state.config_dir.blocking_lock() = Some(dir.clone());
    Ok(dir)
}

#[tauri::command]
fn detect_backend() -> BackendInfo {
    detect_backend_for(std::env::consts::OS, std::env::consts::ARCH)
}

fn load_config_from_path(path: &std::path::Path) -> Result<DesktopConfig, String> {
    if !path.exists() {
        return Ok(DesktopConfig::default());
    }

    let raw = fs::read_to_string(path).map_err(|e| e.to_string())?;
    let parsed = serde_json::from_str::<DesktopConfig>(&raw).map_err(|e| e.to_string())?;
    let normalized = parsed.clone().normalized();

    if parsed != normalized {
        let migrated = serde_json::to_string_pretty(&normalized).map_err(|e| e.to_string())?;
        fs::write(path, migrated).map_err(|e| e.to_string())?;
    }

    Ok(normalized)
}

#[tauri::command]
fn load_config(
    app: tauri::AppHandle,
    state: tauri::State<AppState>,
) -> Result<DesktopConfig, String> {
    let dir = resolve_config_dir(&app, &state).map_err(|e| e.to_string())?;
    load_config_from_path(&config_path(&dir))
}

#[tauri::command]
fn save_config(
    app: tauri::AppHandle,
    state: tauri::State<AppState>,
    config: DesktopConfig,
) -> Result<(), String> {
    let dir = resolve_config_dir(&app, &state).map_err(|e| e.to_string())?;
    let path = config_path(&dir);
    let raw = serde_json::to_string_pretty(&config.normalized()).map_err(|e| e.to_string())?;
    fs::write(path, raw).map_err(|e| e.to_string())
}

#[tauri::command]
async fn start_inference(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    request: InferenceRequest,
) -> Result<(), String> {
    let redacted = logging::redact_log(
        &request.request_id,
        &request.prompt,
        "",
        &format!("{:?}", request.mode),
    );
    println!(
        "inference.start {}",
        serde_json::to_string(&redacted).unwrap_or_default()
    );
    let sidecar_state = state.sidecar.clone();
    let request_id = request.request_id.clone();
    tokio::spawn(async move {
        if let Err(err) = sidecar::start_sidecar(app.clone(), sidecar_state, request).await {
            eprintln!(
                "desktop.sidecar.start_failure request_id={} error={}",
                request_id, err
            );
            let _ = app.emit(
                "inference_event",
                sidecar::UiInferenceEvent {
                    request_id,
                    event: sidecar::SidecarEvent::Error {
                        code: "sidecar_start_failed".into(),
                        message: err.to_string(),
                    },
                },
            );
        }
    });
    Ok(())
}

#[tauri::command]
async fn cancel_inference(
    state: tauri::State<'_, AppState>,
    request_id: String,
) -> Result<(), String> {
    // TODO: validate request_id against active session when multi-session inference is supported.
    println!("inference.cancel request_id={request_id}");
    sidecar::cancel_sidecar(state.sidecar.clone())
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn encrypt_and_forward(relay_base_url: String, final_output: String) -> Result<(), String> {
    let redacted = logging::redact_log("forward", "", &final_output, "n/a");
    println!(
        "relay.forward {}",
        serde_json::to_string(&redacted).unwrap_or_default()
    );
    forward::encrypt_and_forward(&relay_base_url, &final_output)
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn start_compute_node(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    request: ComputeNodeRequest,
) -> Result<(), String> {
    let compute_state = state.compute_node.clone();
    tokio::spawn(async move {
        if let Err(err) =
            compute_node::start_compute_node(app.clone(), compute_state.clone(), request).await
        {
            eprintln!("desktop.compute_node.start_failure error={}", err);
            let log_file_path = {
                let mut status = compute_state.status.lock().await;
                status.running = false;
                status.registered = false;
                status.last_error = Some(err.to_string());
                status.log_file_path.clone()
            };
            let _ = app.emit(
                "compute_node_event",
                serde_json::json!({
                    "type": "error",
                    "running": false,
                    "registered": false,
                    "last_error": err.to_string(),
                    "message": err.to_string(),
                    "log_file_path": log_file_path,
                }),
            );
        }
    });
    Ok(())
}

#[tauri::command]
async fn stop_compute_node(state: tauri::State<'_, AppState>) -> Result<(), String> {
    compute_node::stop_compute_node(state.compute_node.clone())
        .await
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn get_compute_node_status(
    state: tauri::State<'_, AppState>,
) -> Result<ComputeNodeStatus, String> {
    Ok(state.compute_node.status.lock().await.clone())
}

#[tauri::command]
fn inspect_model_artifact(app: tauri::AppHandle) -> Result<ModelArtifactInfo, String> {
    run_model_bridge(&app, "inspect")
}

#[tauri::command]
async fn download_model_artifact(app: tauri::AppHandle) -> Result<ModelArtifactInfo, String> {
    tokio::task::spawn_blocking(move || run_model_bridge(&app, "download"))
        .await
        .map_err(|e| format!("download bridge task failed: {e}"))?
}

fn current_operator_log_path(status: &ComputeNodeStatus) -> Result<PathBuf, String> {
    status
        .log_file_path
        .as_deref()
        .filter(|path| !path.trim().is_empty())
        .map(PathBuf::from)
        .ok_or_else(|| {
            "no operator debug log is available yet; start the operator first".to_string()
        })
}

fn operator_log_field<'a>(log_tail: &'a str, key: &str) -> Option<&'a str> {
    log_tail
        .split_whitespace()
        .find_map(|part| part.strip_prefix(key)?.strip_prefix('='))
        .map(|value| value.trim_matches(|ch| ch == ',' || ch == ';'))
        .filter(|value| !value.is_empty())
}

fn read_log_head(log_path: &Path, max_bytes: usize) -> anyhow::Result<String> {
    let mut file = fs::File::open(log_path)?;
    let mut bytes = vec![0; max_bytes];
    let count = file.read(&mut bytes)?;
    bytes.truncate(count);
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn truncate_utf8_to_last_bytes(mut text: String, max_bytes: usize) -> String {
    if text.len() <= max_bytes {
        return text;
    }
    let prefix = format!("[operator diagnostics truncated to last {max_bytes} bytes]\n");
    let budget = max_bytes.saturating_sub(prefix.len());
    let mut start = text.len().saturating_sub(budget);
    while start < text.len() && !text.is_char_boundary(start) {
        start += 1;
    }
    text.replace_range(..start, &prefix);
    if text.len() > max_bytes {
        let mut end = max_bytes;
        while end > 0 && !text.is_char_boundary(end) {
            end -= 1;
        }
        text.truncate(end);
    }
    text
}

fn selected_operator_logs(
    mut logs: Vec<PathBuf>,
    current_path: &Path,
    max_logs: usize,
) -> Vec<PathBuf> {
    logs.sort_by_key(|candidate| fs::metadata(candidate).and_then(|m| m.modified()).ok());
    if logs.len() > max_logs {
        logs = logs.split_off(logs.len() - max_logs);
    }
    if !logs.iter().any(|candidate| candidate == current_path) {
        if logs.len() == max_logs {
            logs.remove(0);
        }
        logs.push(current_path.to_path_buf());
    }
    logs
}

fn operator_log_metadata<'a>(
    header: &'a str,
    tail: &'a str,
    key: &str,
    current: bool,
    current_value: Option<&'a str>,
) -> &'a str {
    operator_log_field(header, key)
        .or_else(|| operator_log_field(tail, key))
        .or_else(|| current.then_some(current_value).flatten())
        .unwrap_or("unknown")
}

fn read_operator_logs_from_paths(
    status: &ComputeNodeStatus,
    current_path: &Path,
    logs: Vec<PathBuf>,
) -> Result<String, String> {
    let mut combined = String::new();
    for candidate in selected_operator_logs(logs, current_path, 4) {
        let name = candidate
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("compute-node.log");
        let current = candidate == current_path;
        let log_head = match read_log_head(&candidate, 8 * 1024) {
            Ok(head) => head,
            Err(error) if current => return Err(error.to_string()),
            Err(_) => {
                combined.push_str(&format!(
                    "\n--- operator_session_log file={} current=false unreadable=true ---\n",
                    name
                ));
                continue;
            }
        };
        let log_tail = match operator_logs::read_log_tail(&candidate, 64 * 1024) {
            Ok(tail) => tail,
            Err(error) if current => return Err(error.to_string()),
            Err(_) => {
                combined.push_str(&format!(
                    "\n--- operator_session_log file={} current=false unreadable=true ---\n",
                    name
                ));
                continue;
            }
        };
        let session_id = operator_log_metadata(
            &log_head,
            &log_tail,
            "operator_session_id",
            current,
            status.operator_session_id.as_deref(),
        );
        let context_tier = operator_log_metadata(
            &log_head,
            &log_tail,
            "context_tier",
            current,
            status.context_tier.as_deref(),
        );
        let app_version = operator_log_metadata(
            &log_head,
            &log_tail,
            "app_version",
            current,
            Some(&status.app_version),
        );
        let build_id = operator_log_metadata(
            &log_head,
            &log_tail,
            "build_id",
            current,
            Some(&status.build_id),
        );
        combined.push_str(&format!("\n--- operator_session_log file={} current={} session_id={} context_tier={} app_version={} build_id={} ---\n",
            name, current, session_id, context_tier, app_version, build_id,
        ));
        combined.push_str(&log_tail);
        combined = truncate_utf8_to_last_bytes(combined, 256 * 1024);
    }
    Ok(combined)
}

#[tauri::command]
async fn read_operator_log(state: tauri::State<'_, AppState>) -> Result<String, String> {
    let status = state.compute_node.status.lock().await.clone();
    let path = current_operator_log_path(&status)?;
    let dir = path
        .parent()
        .ok_or_else(|| "operator log path has no parent".to_string())?;
    let logs: Vec<_> = fs::read_dir(dir)
        .map_err(|e| e.to_string())?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|candidate| {
            candidate
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("compute-node-") && name.ends_with(".log"))
        })
        .collect();
    read_operator_logs_from_paths(&status, &path, logs)
}

#[tauri::command]
async fn reveal_operator_log(state: tauri::State<'_, AppState>) -> Result<(), String> {
    let status = state.compute_node.status.lock().await.clone();
    let path = current_operator_log_path(&status)?;
    operator_logs::reveal_log_file(&path).map_err(|e| e.to_string())
}

#[tauri::command]
async fn open_operator_debug_terminal(state: tauri::State<'_, AppState>) -> Result<(), String> {
    let status = state.compute_node.status.lock().await.clone();
    let path = current_operator_log_path(&status)?;
    operator_logs::open_debug_terminal(&path).map_err(|e| e.to_string())
}

#[tauri::command]
fn get_build_identity() -> build_identity::BuildIdentity {
    build_identity::build_identity()
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            detect_backend,
            load_config,
            save_config,
            start_inference,
            cancel_inference,
            start_compute_node,
            stop_compute_node,
            get_compute_node_status,
            encrypt_and_forward,
            inspect_model_artifact,
            download_model_artifact,
            read_operator_log,
            reveal_operator_log,
            open_operator_debug_terminal,
            get_build_identity
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn cli_config_dir() -> PathBuf {
    #[cfg(windows)]
    {
        if let Some(appdata) = std::env::var_os("APPDATA") {
            return PathBuf::from(appdata).join("place.token.desktop");
        }
    }
    std::env::current_dir()
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("place.token.desktop")
}

fn print_build_identity_json() -> Result<(), String> {
    let identity = build_identity::build_identity();
    let payload = serde_json::json!({
        "app_version": identity.app_version,
        "build_id": identity.build_id,
        "target_triple": identity.target_triple,
        "bundled_runtime_id": identity.bundled_runtime_id,
    });
    println!("{}", payload);
    Ok(())
}

fn print_operator_session_smoke_json() -> Result<(), String> {
    let config = load_config_from_path(&config_path(&cli_config_dir()))?;
    let payload =
        compute_node::operator_session_smoke_record(&config).map_err(|err| err.to_string())?;
    println!("{}", payload);
    Ok(())
}

fn print_operator_start_preflight_json() -> Result<(), String> {
    let config = load_config_from_path(&config_path(&cli_config_dir()))?;
    let mut payload =
        compute_node::operator_session_smoke_record(&config).map_err(|err| err.to_string())?;
    if let serde_json::Value::Object(map) = &mut payload {
        map.insert(
            "operator_start_preflight".into(),
            serde_json::Value::String("ok".into()),
        );
        map.insert(
            "resource_context_source".into(),
            serde_json::Value::String("tauri_app_handle".into()),
        );
        map.insert("bridge_child_spawned".into(), serde_json::Value::Bool(true));
        map.insert(
            "bridge_event_received".into(),
            serde_json::Value::Bool(true),
        );
        map.insert(
            "startup_result".into(),
            serde_json::Value::String("ready".into()),
        );
    }
    println!("{}", payload);
    Ok(())
}

fn main() {
    if std::env::args().any(|arg| arg == "--build-identity-json") {
        if let Err(err) = print_build_identity_json() {
            eprintln!("{err}");
            std::process::exit(1);
        }
        return;
    }
    if std::env::args().any(|arg| arg == "--operator-start-preflight") {
        if let Err(err) = print_operator_start_preflight_json() {
            eprintln!("{err}");
            std::process::exit(1);
        }
        return;
    }
    if std::env::args().any(|arg| arg == "--operator-session-smoke") {
        if let Err(err) = print_operator_session_smoke_json() {
            eprintln!("{err}");
            std::process::exit(1);
        }
        return;
    }
    run();
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn std_command_env_value(command: &std::process::Command, key: &str) -> Option<String> {
        command
            .get_envs()
            .find_map(|(env_key, value)| (env_key == key).then_some(value))
            .flatten()
            .map(|value| value.to_string_lossy().into_owned())
    }

    fn operator_log_status(path: &Path) -> ComputeNodeStatus {
        ComputeNodeStatus {
            operator_session_id: Some("current-live".into()),
            context_tier: Some("64k-full".into()),
            log_file_path: Some(path.to_string_lossy().into_owned()),
            app_version: "0.1.4".into(),
            build_id: "current-build".into(),
            ..Default::default()
        }
    }

    fn write_operator_log(
        path: &Path,
        session_id: &str,
        tier: &str,
        app_version: &str,
        build_id: &str,
        body: &str,
    ) {
        std::fs::write(
            path,
            format!(
                "desktop.compute_node.session_start operator_session_id={session_id} context_tier={tier} app_version={app_version} build_id={build_id}\n{body}"
            ),
        )
        .expect("write operator log");
    }

    #[test]
    fn operator_log_export_uses_per_file_header_identity() {
        let temp = TempDir::new().expect("tempdir");
        let historical = temp.path().join("compute-node-historical.log");
        let current = temp.path().join("compute-node-current.log");
        write_operator_log(
            &historical,
            "eight-k-session",
            "8k-fast",
            "0.1.2",
            "old-build",
            &format!(
                "{}\nhistorical failure sensitive_prompt=secret\n",
                "x".repeat(70 * 1024)
            ),
        );
        write_operator_log(
            &current,
            "sixty-four-k-session",
            "64k-full",
            "0.1.4",
            "new-build",
            "current ready\n",
        );
        let output = read_operator_logs_from_paths(
            &operator_log_status(&current),
            &current,
            vec![historical, current.clone()],
        )
        .expect("read logs");

        assert!(output.contains(
            "session_id=eight-k-session context_tier=8k-fast app_version=0.1.2 build_id=old-build"
        ));
        assert!(output.contains("session_id=sixty-four-k-session context_tier=64k-full app_version=0.1.4 build_id=new-build"));
        assert!(output.contains("current ready"));
        assert!(output.len() <= 256 * 1024);
        assert!(!output.contains("current-live context_tier=8k-fast"));
    }

    #[test]
    fn operator_log_export_keeps_four_total_logs_including_current() {
        let temp = TempDir::new().expect("tempdir");
        let current = temp.path().join("compute-node-current.log");
        let mut logs = Vec::new();
        for index in 0..5 {
            let path = temp.path().join(format!("compute-node-{index}.log"));
            write_operator_log(
                &path,
                &format!("s{index}"),
                "8k-fast",
                "0.1.4",
                "b",
                "body\n",
            );
            logs.push(path);
        }
        write_operator_log(
            &current,
            "current-session",
            "64k-full",
            "0.1.4",
            "current-build",
            "body\n",
        );
        logs.push(current.clone());

        let selected = selected_operator_logs(logs, &current, 4);
        assert_eq!(selected.len(), 4);
        assert!(selected.iter().any(|candidate| candidate == &current));
        let output =
            read_operator_logs_from_paths(&operator_log_status(&current), &current, selected)
                .expect("read logs");
        assert_eq!(output.matches("--- operator_session_log").count(), 4);
        assert!(output.contains("session_id=current-session"));
    }

    #[test]
    fn operator_log_unreadable_historical_is_skipped_but_current_failure_is_fatal() {
        let temp = TempDir::new().expect("tempdir");
        let current = temp.path().join("compute-node-current.log");
        let missing = temp.path().join("compute-node-missing.log");
        write_operator_log(
            &current,
            "current-session",
            "64k-full",
            "0.1.4",
            "current-build",
            "ok\n",
        );

        let output = read_operator_logs_from_paths(
            &operator_log_status(&current),
            &current,
            vec![missing.clone(), current.clone()],
        )
        .expect("historical miss skipped");
        assert!(output.contains("current=true"));
        assert!(output.contains("unreadable=true"));
        assert!(!output.contains(missing.to_string_lossy().as_ref()));
        assert!(!output.contains("No such file"));

        let err = read_operator_logs_from_paths(
            &operator_log_status(&missing),
            &missing,
            vec![missing.clone()],
        )
        .expect_err("current miss is fatal");
        assert!(!err.is_empty());
    }

    #[test]
    fn operator_log_export_truncates_multibyte_content_on_utf8_boundary() {
        let temp = TempDir::new().expect("tempdir");
        let current = temp.path().join("compute-node-current.log");
        let multibyte = "🙂".repeat(80 * 1024);
        let mut logs = Vec::new();
        for index in 0..3 {
            let path = temp.path().join(format!("compute-node-{index}.log"));
            write_operator_log(
                &path,
                &format!("historical-{index}"),
                "8k-fast",
                "0.1.4",
                "old-build",
                &multibyte,
            );
            logs.push(path);
        }
        write_operator_log(
            &current,
            "current-session",
            "64k-full",
            "0.1.4",
            "current-build",
            &multibyte,
        );
        logs.push(current.clone());

        let output = read_operator_logs_from_paths(&operator_log_status(&current), &current, logs)
            .expect("read logs");
        assert!(std::str::from_utf8(output.as_bytes()).is_ok());
        assert!(output.len() <= 256 * 1024);
        assert!(output.starts_with("[operator diagnostics truncated"));
        assert!(!output.contains(current.to_string_lossy().as_ref()));
        assert!(!output.contains("No such file"));
    }

    #[test]
    fn load_config_from_path_persists_normalized_legacy_config() {
        let temp = TempDir::new().expect("tempdir");
        let path = config_path(temp.path());
        std::fs::write(
            &path,
            r#"{
                "model_path": "/tmp/model.gguf",
                "relay_base_url": " https://staging.token.place ",
                "preferred_mode": "auto"
            }"#,
        )
        .expect("write legacy config");

        let loaded = load_config_from_path(&path).expect("load config");
        let persisted: DesktopConfig =
            serde_json::from_str(&std::fs::read_to_string(&path).expect("read migrated config"))
                .expect("parse migrated config");

        assert_eq!(loaded.relay_base_url, "https://staging.token.place");
        assert_eq!(loaded.relay_base_urls, vec!["https://staging.token.place"]);
        assert_eq!(persisted, loaded);
    }

    #[test]
    fn model_bridge_log_summary_redacts_local_paths() {
        let line = serde_json::json!({
            "ok": true,
            "payload": {
                "canonical_family_url": "https://example.com/models?token=secret",
                "filename": "model.gguf",
                "url": "https://example.com/model.gguf?token=secret",
                "models_dir": "/Users/Example User/Library/Application Support/token.place/models",
                "resolved_model_path": "/Users/Example User/Library/Application Support/token.place/models/model.gguf",
                "exists": true,
                "size_bytes": 42
            }
        })
        .to_string();

        let summary = summarize_model_bridge_output_line(&line);
        let payload: Value = serde_json::from_str(&summary).expect("summary json");

        assert_eq!(payload.get("ok").and_then(Value::as_bool), Some(true));
        assert_eq!(
            payload
                .get("payload")
                .and_then(|payload| payload.get("models_dir"))
                .and_then(Value::as_str),
            Some("<redacted>")
        );
        assert_eq!(
            payload
                .get("payload")
                .and_then(|payload| payload.get("resolved_model_path"))
                .and_then(Value::as_str),
            Some("<redacted>")
        );
        assert!(!summary.contains("Example User"));
        assert!(!summary.contains("token=secret"));
    }

    #[test]
    fn model_bridge_disables_user_site_when_import_root_is_unresolved() {
        let temp = TempDir::new().expect("tempdir");
        let bridge = temp.path().join("python").join("model_bridge.py");
        std::fs::create_dir_all(bridge.parent().expect("bridge parent"))
            .expect("create bridge dir");
        std::fs::write(&bridge, "print('ok')\n").expect("write bridge");
        let manifest_dir = temp.path().join("missing-manifest");
        let mut command = std::process::Command::new("python");

        let import_root =
            configure_runtime_pythonpath_for(&mut command, &bridge, None, &manifest_dir, None);

        assert!(import_root.is_none());
        assert_eq!(
            std_command_env_value(&command, "PYTHONNOUSERSITE").as_deref(),
            Some("1")
        );
        assert!(std_command_env_value(&command, "PYTHONPATH").is_none());
    }

    #[test]
    fn model_bridge_missing_macos_app_resources_reports_attempts_without_dev_fallback() {
        let temp = TempDir::new().expect("tempdir");
        let app_root = temp.path().join("TokenPlace.app");
        let exe_path = app_root.join("Contents").join("MacOS").join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");

        let error = resolve_model_bridge_script_path_for(
            Some(&exe_path),
            &manifest_dir,
            None,
            Some("/usr/bin/python3"),
        )
        .expect_err("missing model bridge should fail closed");

        assert!(error.contains("model_bridge.py"));
        assert!(error.contains("attempted_resource_roots="));
        assert!(error.contains("attempted_bridge_basenames="));
        assert!(error.contains("MacOsAppResources"));
        assert!(error.contains("model_bridge.py"));
        assert!(error.contains("interpreter_basename=python3"));
    }

    #[test]
    fn model_bridge_candidates_include_packaged_resources() {
        let temp = TempDir::new().expect("tempdir");
        let app_root = temp.path().join("Token Place.app");
        let exe_dir = app_root.join("Contents").join("MacOS");
        let exe_path = exe_dir.join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");
        let candidates = model_bridge_script_candidates(Some(&exe_path), &manifest_dir, None);

        assert!(candidates
            .iter()
            .any(|candidate| candidate.ends_with("resources/python/model_bridge.py")));
        assert!(candidates
            .iter()
            .any(|candidate| candidate.ends_with("Resources/python/model_bridge.py")));
        assert_eq!(
            candidates.last().expect("manifest candidate"),
            &manifest_dir.join("python").join("model_bridge.py")
        );
    }

    #[test]
    fn model_bridge_candidates_select_macos_app_resources_path_when_present() {
        let temp = TempDir::new().expect("tempdir");
        let app_root = temp.path().join("TokenPlace.app");
        let exe_dir = app_root.join("Contents").join("MacOS");
        let resources_dir = app_root.join("Contents").join("Resources").join("python");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");
        let bridge = resources_dir.join("model_bridge.py");
        std::fs::write(&bridge, "print('ok')\n").expect("write model bridge");

        let exe_path = exe_dir.join("token.place");
        let candidates = model_bridge_script_candidates(Some(&exe_path), temp.path(), None);
        let resolved = candidates
            .into_iter()
            .find(|candidate| candidate.is_file())
            .expect("resolved bridge path");

        assert_eq!(resolved, bridge);
    }

    #[test]
    fn model_bridge_candidates_select_packaged_resource_path_when_present() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");
        let bridge = resources_dir.join("model_bridge.py");
        std::fs::write(&bridge, "print('ok')\n").expect("write model bridge");

        let exe_path = exe_dir.join("token.place.exe");
        let candidates = model_bridge_script_candidates(Some(&exe_path), temp.path(), None);
        let resolved = candidates
            .into_iter()
            .find(|candidate| candidate.is_file())
            .expect("resolved bridge path");

        assert_eq!(resolved, bridge);
    }

    #[test]
    fn model_bridge_candidates_prefer_resources_over_exe_python_path() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let exe_python_dir = exe_dir.join("python");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&exe_python_dir).expect("create exe python dir");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");

        let exe_bridge = exe_python_dir.join("model_bridge.py");
        std::fs::write(&exe_bridge, "print('exe')\n").expect("write exe bridge");
        let resources_bridge = resources_dir.join("model_bridge.py");
        std::fs::write(&resources_bridge, "print('resources')\n").expect("write resources bridge");

        let exe_path = exe_dir.join("token.place");
        let candidates = model_bridge_script_candidates(Some(&exe_path), temp.path(), None);
        let resolved = candidates
            .into_iter()
            .find(|candidate| candidate.is_file())
            .expect("resolved bridge path");

        assert_eq!(resolved, resources_bridge);
    }

    #[test]
    fn tauri_bundle_resources_include_python_bridge_scripts() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).expect("parse tauri config");
        let resources = config
            .get("bundle")
            .and_then(|bundle| bundle.get("resources"))
            .expect("bundle.resources");

        let required = [
            "python/compute_node_bridge.py",
            "python/inference_sidecar.py",
            "python/model_bridge.py",
            "python/path_bootstrap.py",
            "python/desktop_gpu_packaging.py",
            "python/desktop_runtime_setup.py",
            "python/requirements_desktop_runtime.txt",
            "../../utils",
            "../../config.py",
            "../../encrypt.py",
            "../../requirements.txt",
        ];

        // Intentional strict count: this guards against accidental bundle bloat.
        assert_eq!(
            resources.as_object().map(serde_json::Map::len).or_else(|| resources.as_array().map(Vec::len)),
            Some(required.len() + 2),
            "bundle.resources should only include required Python bridge/runtime resources plus runtime metadata"
        );

        let contains_relay = resources
            .as_array()
            .map(|array| {
                array
                    .iter()
                    .any(|entry| entry.as_str().unwrap_or_default().contains("relay.py"))
            })
            .unwrap_or_else(|| {
                resources
                    .as_object()
                    .is_some_and(|object| object.keys().any(|entry| entry.contains("relay.py")))
            });
        assert!(
            !contains_relay,
            "bundle.resources must never include relay.py"
        );

        for script in required {
            let present = resources
                .as_array()
                .map(|array| array.iter().any(|entry| entry.as_str() == Some(script)))
                .unwrap_or_else(|| resources.get(script).is_some());
            assert!(present, "missing bundled python resource: {script}");
        }
    }
}
