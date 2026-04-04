mod backend;
mod config;
mod forward;
mod sidecar;

use anyhow::Result;
use backend::{detect_backend, BackendPreference, ComputeMode};
use config::{load_config, save_config, DesktopConfig};
use sidecar::{parse_event, sanitize_log_line, InferenceRequest, SidecarEvent};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::{Emitter, Manager, State};

#[derive(Default)]
struct RuntimeState {
    child: Arc<Mutex<Option<Child>>>,
}

#[tauri::command]
fn get_config() -> DesktopConfig {
    load_config()
}

#[tauri::command]
fn update_config(config: DesktopConfig) -> Result<(), String> {
    save_config(&config).map_err(|e| e.to_string())
}

#[tauri::command]
fn detect_preferred_backend(mode: ComputeMode) -> BackendPreference {
    detect_backend(std::env::consts::OS, std::env::consts::ARCH, mode)
}

fn spawn_sidecar(command_line: &str) -> Result<Child> {
    let parts: Vec<&str> = command_line.split_whitespace().collect();
    let (bin, args) = parts.split_first().ok_or_else(|| anyhow::anyhow!("empty sidecar command"))?;
    let child = Command::new(bin)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    Ok(child)
}

#[tauri::command]
fn start_inference(
    app: tauri::AppHandle,
    state: State<'_, RuntimeState>,
    req: InferenceRequest,
    config: DesktopConfig,
) -> Result<(), String> {
    let mut child = spawn_sidecar(&config.sidecar_command).map_err(|e| e.to_string())?;

    if let Some(stdin) = child.stdin.as_mut() {
        let payload = serde_json::to_string(&req).map_err(|e| e.to_string())?;
        stdin.write_all(payload.as_bytes()).map_err(|e| e.to_string())?;
        stdin.write_all(b"\n").map_err(|e| e.to_string())?;
    }

    let stdout = child.stdout.take().ok_or_else(|| "missing sidecar stdout".to_string())?;
    let stderr = child.stderr.take().ok_or_else(|| "missing sidecar stderr".to_string())?;

    *state.child.lock().map_err(|e| e.to_string())? = Some(child);

    let app_clone = app.clone();
    tauri::async_runtime::spawn(async move {
        let reader = BufReader::new(stdout);
        for line in reader.lines().map_while(Result::ok) {
            match parse_event(&line) {
                Ok(event) => {
                    let _ = app_clone.emit("inference_event", event);
                }
                Err(_) => {
                    let _ = app_clone.emit(
                        "inference_event",
                        SidecarEvent::Error {
                            code: "parse_error".to_string(),
                            message: "Invalid sidecar event".to_string(),
                        },
                    );
                }
            }
        }
    });

    let app_err = app.clone();
    tauri::async_runtime::spawn(async move {
        let reader = BufReader::new(stderr);
        for line in reader.lines().map_while(Result::ok) {
            let _ = app_err.emit("sidecar_log", sanitize_log_line(&line));
        }
    });

    Ok(())
}

#[tauri::command]
fn cancel_inference(state: State<'_, RuntimeState>) -> Result<(), String> {
    if let Some(child) = state.child.lock().map_err(|e| e.to_string())?.as_mut() {
        child.kill().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[derive(serde::Deserialize)]
struct ForwardRequest {
    relay_base_url: String,
    server_public_key: String,
    final_output: String,
}

#[tauri::command]
async fn encrypt_and_forward(request: ForwardRequest) -> Result<(), String> {
    let envelope = forward::build_faucet_envelope(&request.final_output, &request.server_public_key)
        .map_err(|e| e.to_string())?;

    let client = reqwest::Client::new();
    let endpoint = format!("{}/faucet", request.relay_base_url.trim_end_matches('/'));
    let response = client
        .post(endpoint)
        .json(&envelope)
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if !response.status().is_success() {
        return Err(format!("relay returned {}", response.status()));
    }

    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(RuntimeState::default())
        .invoke_handler(tauri::generate_handler![
            get_config,
            update_config,
            detect_preferred_backend,
            start_inference,
            cancel_inference,
            encrypt_and_forward
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};

    #[test]
    fn fake_sidecar_emits_parseable_events() {
        let mut child = Command::new("python3")
            .arg("../fake_sidecar.py")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn fake sidecar");

        let stdin = child.stdin.as_mut().expect("stdin");
        let req = serde_json::json!({
            "model_path": "/tmp/model.gguf",
            "prompt": "hello",
            "backend": "cpu"
        });
        stdin
            .write_all(format!("{req}\n").as_bytes())
            .expect("write request");

        let stdout = child.stdout.take().expect("stdout");
        let reader = BufReader::new(stdout);
        let mut seen_done = false;
        for line in reader.lines().map_while(Result::ok) {
            let event = parse_event(&line).expect("event");
            if matches!(event, SidecarEvent::Done) {
                seen_done = true;
                break;
            }
        }

        assert!(seen_done);
    }
}
