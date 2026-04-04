pub mod backend;
pub mod config;
pub mod forward;
pub mod logging;
pub mod sidecar;

#[cfg(feature = "app")]
mod app {
    use std::sync::Arc;

    use serde::Serialize;
    use tauri::{AppHandle, Emitter, State};
    use tokio::sync::Mutex;

    use crate::config::AppConfig;
    use crate::sidecar::{InferenceEvent, SidecarManager};

    #[derive(Default)]
    struct AppState {
        manager: Arc<Mutex<SidecarManager>>,
    }

    #[derive(Debug, Serialize)]
    #[serde(rename_all = "camelCase")]
    struct BackendInfo {
        backend: String,
        preferred_label: String,
        reason: String,
    }

    #[tauri::command]
    fn detect_backend() -> BackendInfo {
        let detected = crate::backend::detect_backend(std::env::consts::OS, std::env::consts::ARCH);
        BackendInfo {
            backend: detected.backend.to_string(),
            preferred_label: detected.label,
            reason: detected.reason,
        }
    }

    #[tauri::command]
    fn load_config(app: AppHandle) -> Result<AppConfig, String> {
        crate::config::load_config(&app).map_err(|e| e.to_string())
    }

    #[tauri::command]
    fn save_config(app: AppHandle, config: AppConfig) -> Result<(), String> {
        crate::config::save_config(&app, &config).map_err(|e| e.to_string())
    }

    #[tauri::command]
    async fn start_inference(
        app: AppHandle,
        state: State<'_, AppState>,
        model_path: String,
        prompt: String,
        compute_mode: String,
    ) -> Result<String, String> {
        let mut mgr = state.manager.lock().await;
        let run_id = mgr
            .start(model_path, prompt, compute_mode)
            .await
            .map_err(|e| e.to_string())?;
        let rx = mgr
            .subscribe()
            .ok_or_else(|| "missing stream receiver".to_string())?;
        spawn_forwarder(app, rx);
        Ok(run_id)
    }

    #[tauri::command]
    async fn cancel_inference(state: State<'_, AppState>, run_id: String) -> Result<(), String> {
        let mut mgr = state.manager.lock().await;
        mgr.cancel(&run_id).await.map_err(|e| e.to_string())
    }

    #[derive(Serialize)]
    struct ForwardResult {
        status: String,
        relay_response: String,
    }

    #[tauri::command]
    async fn encrypt_and_forward_output(
        relay_base_url: String,
        plaintext_output: String,
    ) -> Result<ForwardResult, String> {
        crate::forward::encrypt_and_forward(&relay_base_url, &plaintext_output)
            .await
            .map(|relay_response| ForwardResult {
                status: "ok".to_string(),
                relay_response,
            })
            .map_err(|e| e.to_string())
    }

    fn spawn_forwarder(app: AppHandle, mut rx: tokio::sync::mpsc::Receiver<InferenceEvent>) {
        tauri::async_runtime::spawn(async move {
            while let Some(event) = rx.recv().await {
                let _ = app.emit("inference_event", event);
            }
        });
    }

    #[cfg_attr(mobile, tauri::mobile_entry_point)]
    pub fn run() {
        tauri::Builder::default()
            .plugin(tauri_plugin_opener::init())
            .manage(AppState::default())
            .invoke_handler(tauri::generate_handler![
                detect_backend,
                load_config,
                save_config,
                start_inference,
                cancel_inference,
                encrypt_and_forward_output
            ])
            .run(tauri::generate_context!())
            .expect("error while running token.place desktop");
    }
}

#[cfg(feature = "app")]
pub use app::run;

#[cfg(not(feature = "app"))]
pub fn run() {
    panic!("desktop app runtime requires `app` feature");
}
