mod backend;
mod config;
mod relay;
mod sidecar;

use std::sync::Arc;

use backend::{detect_backend_info, ComputeMode};
use config::{DesktopConfig, DesktopConfigStore};
use relay::forward_output_encrypted;
use sidecar::{InferenceRequest, SidecarManager};
use tauri::{AppHandle, Manager};
use tokio::sync::Mutex;

#[derive(Default)]
struct AppState {
    sidecar: Arc<Mutex<SidecarManager>>,
}

#[tauri::command]
fn load_desktop_config(app: AppHandle) -> Result<DesktopConfig, String> {
    DesktopConfigStore::new(&app)
        .load()
        .map_err(|err| err.to_string())
}

#[tauri::command]
fn save_desktop_config(app: AppHandle, next_config: DesktopConfig) -> Result<(), String> {
    DesktopConfigStore::new(&app)
        .save(&next_config)
        .map_err(|err| err.to_string())
}

#[tauri::command]
fn detect_backend(preferred_mode: ComputeMode) -> backend::BackendInfo {
    detect_backend_info(preferred_mode)
}

#[tauri::command]
async fn start_inference(
    app: AppHandle,
    state: tauri::State<'_, AppState>,
    request: InferenceRequest,
) -> Result<(), String> {
    let window = app
        .get_webview_window("main")
        .ok_or("Missing main window")?;
    state
        .sidecar
        .lock()
        .await
        .start(window, request)
        .await
        .map_err(|err| err.to_string())
}

#[tauri::command]
async fn cancel_inference(state: tauri::State<'_, AppState>) -> Result<(), String> {
    state
        .sidecar
        .lock()
        .await
        .cancel()
        .await
        .map_err(|err| err.to_string())
}

#[tauri::command]
async fn forward_output_encrypted(
    relay_base_url: String,
    output: String,
) -> Result<String, String> {
    forward_output_encrypted(&relay_base_url, &output)
        .await
        .map(|result| format!("status={} encrypted={}", result.status, result.encrypted))
        .map_err(|err| err.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            load_desktop_config,
            save_desktop_config,
            detect_backend,
            start_inference,
            cancel_inference,
            forward_output_encrypted
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

fn main() {
    run();
}
