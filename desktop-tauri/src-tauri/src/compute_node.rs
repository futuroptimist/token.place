use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComputeNodeRequest {
    pub relay_base_url: String,
    pub model_path: String,
    pub mode: ComputeMode,
    pub stream_enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ComputeNodeStatus {
    pub registered: bool,
    pub running: bool,
    pub active_relay_url: String,
    pub backend_mode: String,
    pub model_path: String,
    pub stream_enabled: bool,
    pub last_error: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type")]
enum BridgeEvent {
    #[serde(rename = "status")]
    Status {
        registered: bool,
        running: bool,
        active_relay_url: String,
        backend_mode: String,
        model_path: String,
        stream_enabled: bool,
        last_error: String,
    },
    #[serde(rename = "error")]
    Error { message: String },
    #[serde(rename = "stopped")]
    Stopped,
    #[serde(rename = "started")]
    Started { relay_target: String },
}

#[derive(Clone, Default)]
pub struct ComputeNodeState {
    pub child: Arc<Mutex<Option<Child>>>,
    pub stdin: Arc<Mutex<Option<ChildStdin>>>,
    pub status: Arc<Mutex<ComputeNodeStatus>>,
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

fn resolve_compute_bridge_script() -> String {
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

pub async fn start_compute_node(
    app: AppHandle,
    state: ComputeNodeState,
    request: ComputeNodeRequest,
) -> anyhow::Result<()> {
    {
        let mut child_slot = state.child.lock().await;
        if child_slot
            .as_mut()
            .is_some_and(|child| child.try_wait().ok().flatten().is_none())
        {
            anyhow::bail!("compute node already running; stop before starting a new one");
        }
        *child_slot = None;
        *state.stdin.lock().await = None;
    }

    let bridge_script = std::env::var("TOKEN_PLACE_COMPUTE_BRIDGE")
        .unwrap_or_else(|_| resolve_compute_bridge_script());

    let mut cmd = build_bridge_command(&bridge_script);
    cmd.arg("--relay-url")
        .arg(&request.relay_base_url)
        .arg("--model")
        .arg(&request.model_path)
        .arg("--mode")
        .arg(format!("{:?}", request.mode).to_lowercase());
    if request.stream_enabled {
        cmd.arg("--stream-enabled");
    }

    let mut child = cmd
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute bridge stdout"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute bridge stdin"))?;

    {
        let mut child_slot = state.child.lock().await;
        *child_slot = Some(child);
        *state.stdin.lock().await = Some(stdin);
    }

    let mut lines = BufReader::new(stdout).lines();
    while let Some(line) = lines.next_line().await? {
        if let Ok(event) = serde_json::from_str::<BridgeEvent>(&line) {
            let mut status = state.status.lock().await;
            match event {
                BridgeEvent::Status {
                    registered,
                    running,
                    active_relay_url,
                    backend_mode,
                    model_path,
                    stream_enabled,
                    last_error,
                } => {
                    *status = ComputeNodeStatus {
                        registered,
                        running,
                        active_relay_url,
                        backend_mode,
                        model_path,
                        stream_enabled,
                        last_error,
                    };
                }
                BridgeEvent::Error { message } => {
                    status.last_error = message;
                }
                BridgeEvent::Stopped => {
                    status.running = false;
                }
                BridgeEvent::Started { relay_target } => {
                    status.running = true;
                    status.active_relay_url = relay_target;
                }
            }
            app.emit("compute_node_status", status.clone())?;
        }
    }

    *state.child.lock().await = None;
    *state.stdin.lock().await = None;
    state.status.lock().await.running = false;
    Ok(())
}

pub async fn stop_compute_node(state: ComputeNodeState) -> anyhow::Result<()> {
    if let Some(stdin) = state.stdin.lock().await.as_mut() {
        stdin.write_all(b"{\"type\":\"cancel\"}\n").await?;
        stdin.flush().await?;
    }

    if let Some(child) = state.child.lock().await.as_mut() {
        let _ = child.kill().await;
    }

    *state.child.lock().await = None;
    *state.stdin.lock().await = None;
    state.status.lock().await.running = false;
    Ok(())
}
