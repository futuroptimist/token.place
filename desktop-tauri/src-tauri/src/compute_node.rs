use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComputeNodeRequest {
    pub model_path: String,
    pub relay_base_url: String,
    pub mode: ComputeMode,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ComputeNodeStatus {
    pub running: bool,
    pub registered: bool,
    pub active_relay_url: String,
    pub relay_target: String,
    pub relay_port: Option<i64>,
    pub backend_mode: String,
    pub model_path: String,
    pub last_error: Option<String>,
}

#[derive(Clone, Default)]
pub struct ComputeNodeState {
    pub child: Arc<Mutex<Option<Child>>>,
    pub stdin: Arc<Mutex<Option<ChildStdin>>>,
    pub status: Arc<Mutex<ComputeNodeStatus>>,
    pub lifecycle_lock: Arc<Mutex<()>>,
}

fn build_bridge_command(bridge_path: &str) -> Command {
    let path = Path::new(bridge_path);
    let is_python = path
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("py"));

    if is_python {
        let python_bin =
            std::env::var("TOKEN_PLACE_SIDECAR_PYTHON").unwrap_or_else(|_| "python3".into());
        let mut cmd = Command::new(python_bin);
        cmd.arg(bridge_path);
        return cmd;
    }

    Command::new(bridge_path)
}

fn resolve_bridge_script() -> String {
    let mut candidates = Vec::new();

    if let Ok(exe_path) = std::env::current_exe() {
        if let Some(exe_dir) = exe_path.parent() {
            candidates.push(exe_dir.join("python").join("compute_node_bridge.py"));
            candidates.push(
                exe_dir
                    .join("resources")
                    .join("python")
                    .join("compute_node_bridge.py"),
            );
            if let Some(parent_dir) = exe_dir.parent() {
                candidates.push(
                    parent_dir
                        .join("Resources")
                        .join("python")
                        .join("compute_node_bridge.py"),
                );
                candidates.push(
                    parent_dir
                        .join("resources")
                        .join("python")
                        .join("compute_node_bridge.py"),
                );
            }
        }
    }

    candidates.push(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("python")
            .join("compute_node_bridge.py"),
    );

    for candidate in candidates {
        if candidate.is_file() {
            return candidate.to_string_lossy().into_owned();
        }
    }

    "python/compute_node_bridge.py".into()
}

fn update_status_from_event(status: &mut ComputeNodeStatus, payload: &Value) {
    if let Some(running) = payload.get("running").and_then(Value::as_bool) {
        status.running = running;
    }
    if let Some(registered) = payload.get("registered").and_then(Value::as_bool) {
        status.registered = registered;
    }
    if let Some(active_relay_url) = payload.get("active_relay_url").and_then(Value::as_str) {
        status.active_relay_url = active_relay_url.into();
    }
    if let Some(relay_target) = payload.get("relay_target").and_then(Value::as_str) {
        status.relay_target = relay_target.into();
    }
    if payload.get("relay_port").is_some() {
        status.relay_port = payload.get("relay_port").and_then(Value::as_i64);
    }
    if let Some(backend_mode) = payload.get("backend_mode").and_then(Value::as_str) {
        status.backend_mode = backend_mode.into();
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
    if payload.get("type").and_then(Value::as_str) == Some("error") {
        status.last_error = payload
            .get("message")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
            .or_else(|| Some("compute-node bridge error".into()));
    }
}

pub async fn start_compute_node(
    app: AppHandle,
    state: ComputeNodeState,
    request: ComputeNodeRequest,
) -> anyhow::Result<()> {
    let _lifecycle_lock = state.lifecycle_lock.lock().await;

    {
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

    let bridge_script = resolve_bridge_script();
    let spawn_result = build_bridge_command(&bridge_script)
        .arg("--model")
        .arg(&request.model_path)
        .arg("--mode")
        .arg(format!("{:?}", request.mode).to_lowercase())
        .arg("--relay-url")
        .arg(&request.relay_base_url)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();

    let mut child = match spawn_result {
        Ok(child) => child,
        Err(err) => {
            {
                let mut status = state.status.lock().await;
                *status = ComputeNodeStatus {
                    running: false,
                    registered: false,
            active_relay_url: request.relay_base_url.clone(),
            relay_target: request.relay_base_url.clone(),
            relay_port: None,
            backend_mode: format!("{:?}", request.mode).to_lowercase(),
            model_path: request.model_path.clone(),
            last_error: Some(format!("failed to start compute-node bridge: {err}")),
                };
            }
            *state.child.lock().await = None;
            *state.stdin.lock().await = None;
            anyhow::bail!("failed to spawn compute-node bridge: {err}");
        }
    };

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute-node bridge stdout"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute-node bridge stdin"))?;

    {
        let mut child_slot = state.child.lock().await;
        *child_slot = Some(child);
        let mut stdin_slot = state.stdin.lock().await;
        *stdin_slot = Some(stdin);
        let mut status = state.status.lock().await;
        *status = ComputeNodeStatus {
            running: true,
            registered: false,
            active_relay_url: request.relay_base_url.clone(),
            relay_target: request.relay_base_url.clone(),
            relay_port: None,
            backend_mode: format!("{:?}", request.mode).to_lowercase(),
            model_path: request.model_path.clone(),
            last_error: None,
        };
    }

    let mut lines = BufReader::new(stdout).lines();
    while let Some(line) = lines.next_line().await? {
        if let Ok(payload) = serde_json::from_str::<Value>(&line) {
            {
                let mut status = state.status.lock().await;
                update_status_from_event(&mut status, &payload);
            }
            app.emit("compute_node_event", payload)?;
        }
    }

    {
        let mut status = state.status.lock().await;
        status.running = false;
        status.registered = false;
    }
    *state.child.lock().await = None;
    *state.stdin.lock().await = None;

    Ok(())
}

pub async fn stop_compute_node(state: ComputeNodeState) -> anyhow::Result<()> {
    let _lifecycle_lock = state.lifecycle_lock.lock().await;

    if let Some(stdin) = state.stdin.lock().await.as_mut() {
        stdin.write_all(b"{\"type\":\"cancel\"}\n").await?;
        stdin.flush().await?;
    }

    let mut should_kill = false;
    for _ in 0..20 {
        let mut child_lock = state.child.lock().await;
        let Some(child) = child_lock.as_mut() else {
            break;
        };

        if child.try_wait()?.is_some() {
            *child_lock = None;
            *state.stdin.lock().await = None;
            let mut status = state.status.lock().await;
            status.running = false;
            status.registered = false;
            return Ok(());
        }

        should_kill = true;
        drop(child_lock);
        tokio::time::sleep(Duration::from_millis(50)).await;
    }

    if should_kill {
        let mut child_lock = state.child.lock().await;
        if let Some(child) = child_lock.as_mut() {
            let _ = child.kill().await;
        }
    }

    *state.child.lock().await = None;
    *state.stdin.lock().await = None;
    {
        let mut status = state.status.lock().await;
        status.running = false;
        status.registered = false;
    }
    Ok(())
}
