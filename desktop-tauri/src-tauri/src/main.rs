mod backend;
mod config;
mod forward;
mod keygen;
mod logging;
mod model_bridge;
mod sidecar;

use backend::{detect_backend_for, BackendInfo};
use config::{config_path, DesktopConfig};
use model_bridge::{
    download_model, fetch_model_metadata, DownloadModelResponse, ModelMetadataResponse,
};
use sidecar::{InferenceRequest, SidecarState};
use std::fs;
use std::path::PathBuf;
use tauri::Manager;
use tokio::sync::Mutex;

#[derive(Default)]
struct AppState {
    sidecar: SidecarState,
    config_dir: Mutex<Option<PathBuf>>,
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
fn load_model_metadata() -> Result<ModelMetadataResponse, String> {
    fetch_model_metadata()
}

#[tauri::command]
fn download_runtime_model() -> Result<DownloadModelResponse, String> {
    download_model()
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            detect_backend,
            load_config,
            save_config,
            load_model_metadata,
            download_runtime_model,
            start_inference,
            cancel_inference,
            encrypt_and_forward
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn main() {
    run();
}
