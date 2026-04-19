mod backend;
mod compute_node;
mod config;
mod desktop_runtime_bootstrap;
mod forward;
mod keygen;
mod logging;
mod python_runtime;
mod sidecar;
mod subprocess_logging;

use backend::{detect_backend_for, BackendInfo};
use compute_node::{ComputeNodeRequest, ComputeNodeState, ComputeNodeStatus};
use config::{config_path, DesktopConfig};
use serde::{Deserialize, Serialize};
use sidecar::{InferenceRequest, SidecarState};
use std::fs;
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

fn find_existing_bridge_script_path() -> Option<PathBuf> {
    let current_exe = std::env::current_exe().ok();
    let candidates = model_bridge_script_candidates(
        current_exe.as_deref(),
        Path::new(env!("CARGO_MANIFEST_DIR")),
    );
    candidates.into_iter().find(|path| path.is_file())
}

fn model_bridge_script_candidates(exe_path: Option<&Path>, manifest_dir: &Path) -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    if let Some(current_exe) = exe_path {
        if let Some(exe_dir) = current_exe.parent() {
            candidates.push(
                exe_dir
                    .join("resources")
                    .join("python")
                    .join("model_bridge.py"),
            );
            candidates.push(exe_dir.join("python").join("model_bridge.py"));
            candidates.push(
                exe_dir
                    .join("..")
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

    candidates.push(manifest_dir.join("python").join("model_bridge.py"));
    candidates
}

fn resolve_model_bridge_script_path() -> Result<PathBuf, String> {
    find_existing_bridge_script_path().ok_or_else(|| {
        "unable to locate model bridge script relative to executable/resources or development source tree".into()
    })
}

fn configure_runtime_pythonpath(command: &mut std::process::Command, bridge_script: &Path) {
    // NOTE: CARGO_MANIFEST_DIR is compile-time and primarily helps local/dev launches.
    // Packaged end-user launches rely on python/path_bootstrap.py for runtime import roots.
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    if let Some(import_root) =
        python_runtime::resolve_runtime_import_root(Some(bridge_script), manifest_dir)
    {
        command.env("TOKEN_PLACE_PYTHON_IMPORT_ROOT", &import_root);
        match std::env::var("PYTHONPATH") {
            Ok(existing) if !existing.trim().is_empty() => {
                let mut components = vec![import_root.clone()];
                components.extend(std::env::split_paths(&existing));
                if let Ok(joined) = std::env::join_paths(components) {
                    command.env("PYTHONPATH", joined);
                } else {
                    command.env("PYTHONPATH", import_root);
                }
            }
            _ => {
                command.env("PYTHONPATH", import_root);
            }
        }
    }
}

fn run_model_bridge(action: &str) -> Result<ModelArtifactInfo, String> {
    let launcher = python_runtime::resolve_python_launcher("TOKEN_PLACE_PYTHON")
        .map_err(|e| format!("unable to resolve Python launcher for model bridge: {e}"))?;
    let bridge_script = resolve_model_bridge_script_path()?;
    let mut bridge_command =
        launcher.command_for_script_blocking(bridge_script.to_str().unwrap_or_default());
    configure_runtime_pythonpath(&mut bridge_command, &bridge_script);
    let output = bridge_command
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
            {
                let mut status = compute_state.status.lock().await;
                status.running = false;
                status.registered = false;
                status.last_error = Some(err.to_string());
            }
            let _ = app.emit(
                "compute_node_event",
                serde_json::json!({
                    "type": "error",
                    "running": false,
                    "registered": false,
                    "last_error": err.to_string(),
                    "message": err.to_string(),
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

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

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
        let candidates = model_bridge_script_candidates(Some(&exe_path), &manifest_dir);

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
    fn model_bridge_candidates_select_packaged_resource_path_when_present() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");
        let bridge = resources_dir.join("model_bridge.py");
        std::fs::write(&bridge, "print('ok')\n").expect("write model bridge");

        let exe_path = exe_dir.join("token.place.exe");
        let candidates = model_bridge_script_candidates(Some(&exe_path), temp.path());
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
        let candidates = model_bridge_script_candidates(Some(&exe_path), temp.path());
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
            .and_then(serde_json::Value::as_array)
            .expect("bundle.resources array");

        let required = [
            "python/compute_node_bridge.py",
            "python/inference_sidecar.py",
            "python/model_bridge.py",
            "python/path_bootstrap.py",
            "../../utils",
            "../../config.py",
            "../../encrypt.py",
        ];

        // Intentional strict count: this guards against accidental bundle bloat.
        assert_eq!(
            resources.len(),
            required.len(),
            "bundle.resources should only include required Python bridge/runtime resources"
        );

        for script in required {
            assert!(
                resources.iter().any(|entry| entry.as_str() == Some(script)),
                "missing bundled python resource: {script}"
            );
        }
    }
}
