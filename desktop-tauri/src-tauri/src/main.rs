mod backend;
mod compute_node;
mod config;
mod forward;
mod keygen;
mod logging;
mod sidecar;

use backend::{detect_backend_for, BackendInfo};
use compute_node::{ComputeNodeRequest, ComputeNodeState, ComputeNodeStatus};
use config::{config_path, DesktopConfig};
use serde::{Deserialize, Serialize};
use sidecar::{InferenceRequest, SidecarState};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use tauri::Manager;
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

fn find_existing_bridge_script_path() -> Option<PathBuf> {
    let mut candidates = Vec::new();

    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(exe_dir) = current_exe.parent() {
            candidates.push(exe_dir.join("python").join("model_bridge.py"));
            candidates.push(
                exe_dir
                    .join("resources")
                    .join("python")
                    .join("model_bridge.py"),
            );
            candidates.push(
                exe_dir
                    .join("..")
                    .join("Resources")
                    .join("python")
                    .join("model_bridge.py"),
            );
        }
    }

    candidates.push(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("python")
            .join("model_bridge.py"),
    );

    candidates.into_iter().find(|path| path.is_file())
}

fn resolve_model_bridge_script_path() -> Result<PathBuf, String> {
    find_existing_bridge_script_path().ok_or_else(|| {
        "unable to locate model bridge script relative to executable/resources or development source tree".into()
    })
}

fn run_model_bridge(action: &str) -> Result<ModelArtifactInfo, String> {
    let python_bin = std::env::var("TOKEN_PLACE_PYTHON").unwrap_or_else(|_| "python".into());
    let bridge_script = resolve_model_bridge_script_path()?;
    let output = Command::new(python_bin)
        .arg(&bridge_script)
        .arg(action)
        .output()
        .map_err(|e| format!("unable to run model bridge: {e}"))?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
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

#[tauri::command]
fn load_config(
    app: tauri::AppHandle,
    state: tauri::State<AppState>,
) -> Result<DesktopConfig, String> {
    let dir = resolve_config_dir(&app, &state).map_err(|e| e.to_string())?;
    let path = config_path(&dir);
    if !path.exists() {
        return Ok(DesktopConfig::default());
    }
    let raw = fs::read_to_string(path).map_err(|e| e.to_string())?;
    serde_json::from_str(&raw).map_err(|e| e.to_string())
}

#[tauri::command]
fn save_config(
    app: tauri::AppHandle,
    state: tauri::State<AppState>,
    config: DesktopConfig,
) -> Result<(), String> {
    let dir = resolve_config_dir(&app, &state).map_err(|e| e.to_string())?;
    let path = config_path(&dir);
    let raw = serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?;
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
    sidecar::start_sidecar(app, state.sidecar.clone(), request)
        .await
        .map_err(|e| e.to_string())
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
    compute_node::start_compute_node(app, state.compute_node.clone(), request)
        .await
        .map_err(|e| e.to_string())
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
fn inspect_model_artifact() -> Result<ModelArtifactInfo, String> {
    run_model_bridge("inspect")
}

#[tauri::command]
async fn download_model_artifact() -> Result<ModelArtifactInfo, String> {
    tokio::task::spawn_blocking(|| run_model_bridge("download"))
        .await
        .map_err(|e| format!("download bridge task failed: {e}"))?
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
            download_model_artifact
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn main() {
    run();
}
