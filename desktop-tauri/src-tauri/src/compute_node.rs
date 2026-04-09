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
pub struct ComputeNodeStartRequest {
    pub relay_base_url: String,
    pub model_path: String,
    pub mode: ComputeMode,
    pub streaming_enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct OperatorStatus {
    pub running: bool,
    pub registered: bool,
    pub relay_url: String,
    pub backend_mode: String,
    pub model_path: String,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
enum BridgeEvent {
    #[serde(rename = "status")]
    Status {
        registered: bool,
        relay_url: String,
        backend_mode: String,
        model_path: String,
        last_error: Option<String>,
    },
    #[serde(rename = "error")]
    Error { message: String },
    #[serde(rename = "stopped")]
    Stopped,
}

#[derive(Clone, Default)]
pub struct ComputeNodeState {
    child: Arc<Mutex<Option<Child>>>,
    stdin: Arc<Mutex<Option<ChildStdin>>>,
    status: Arc<Mutex<OperatorStatus>>,
}

fn resolve_bridge_script() -> String {
    if let Ok(override_path) = std::env::var("TOKEN_PLACE_COMPUTE_NODE_BRIDGE") {
        return override_path;
    }

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

    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("python")
        .join("compute_node_bridge.py")
        .to_string_lossy()
        .into_owned()
}

fn build_bridge_command(script: &str) -> Command {
    let path = Path::new(script);
    let is_python = path
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("py"));

    if is_python {
        let python_bin =
            std::env::var("TOKEN_PLACE_SIDECAR_PYTHON").unwrap_or_else(|_| "python3".into());
        let mut cmd = Command::new(python_bin);
        cmd.arg(script);
        return cmd;
    }

    Command::new(script)
}

pub async fn start_compute_node(
    app: AppHandle,
    state: ComputeNodeState,
    request: ComputeNodeStartRequest,
) -> anyhow::Result<()> {
    {
        let mut child_slot = state.child.lock().await;
        if child_slot
            .as_mut()
            .is_some_and(|child| child.try_wait().ok().flatten().is_none())
        {
            anyhow::bail!("compute node already running");
        }
        *child_slot = None;
        *state.stdin.lock().await = None;
    }

    let bridge_script = resolve_bridge_script();
    let mut child = build_bridge_command(&bridge_script)
        .arg("--relay-url")
        .arg(&request.relay_base_url)
        .arg("--model-path")
        .arg(&request.model_path)
        .arg("--mode")
        .arg(format!("{:?}", request.mode).to_lowercase())
        .arg("--streaming")
        .arg(if request.streaming_enabled { "1" } else { "0" })
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing bridge stdout"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing bridge stdin"))?;

    {
        let mut status = state.status.lock().await;
        *status = OperatorStatus {
            running: true,
            registered: false,
            relay_url: request.relay_base_url,
            backend_mode: format!("{:?}", request.mode).to_lowercase(),
            model_path: request.model_path.clone(),
            last_error: None,
        };
    }

    {
        let mut child_slot = state.child.lock().await;
        *child_slot = Some(child);
        let mut stdin_slot = state.stdin.lock().await;
        *stdin_slot = Some(stdin);
    }

    tokio::spawn(watch_bridge_stdout(app, state.clone(), stdout));
    Ok(())
}

async fn watch_bridge_stdout(
    app: AppHandle,
    state: ComputeNodeState,
    stdout: tokio::process::ChildStdout,
) {
    let mut reader = BufReader::new(stdout).lines();
    while let Ok(Some(line)) = reader.next_line().await {
        let parsed = serde_json::from_str::<BridgeEvent>(&line);
        if let Ok(event) = parsed {
            match event {
                BridgeEvent::Status {
                    registered,
                    relay_url,
                    backend_mode,
                    model_path,
                    last_error,
                } => {
                    let next_status = OperatorStatus {
                        running: true,
                        registered,
                        relay_url,
                        backend_mode,
                        model_path,
                        last_error,
                    };
                    *state.status.lock().await = next_status.clone();
                    let _ = app.emit("compute_node_status", next_status);
                }
                BridgeEvent::Error { message } => {
                    let mut status = state.status.lock().await;
                    status.last_error = Some(message.clone());
                    let _ = app.emit("compute_node_status", status.clone());
                }
                BridgeEvent::Stopped => {
                    let mut status = state.status.lock().await;
                    status.running = false;
                    let _ = app.emit("compute_node_status", status.clone());
                }
            }
        }
    }

    {
        let mut child_slot = state.child.lock().await;
        *child_slot = None;
    }
    {
        let mut stdin_slot = state.stdin.lock().await;
        *stdin_slot = None;
    }
    {
        let mut status = state.status.lock().await;
        status.running = false;
    }
}

pub async fn stop_compute_node(state: ComputeNodeState) -> anyhow::Result<()> {
    if let Some(stdin) = state.stdin.lock().await.as_mut() {
        stdin.write_all(b"{\"type\":\"stop\"}\n").await?;
        stdin.flush().await?;
    }

    let mut child_slot = state.child.lock().await;
    if let Some(child) = child_slot.as_mut() {
        let _ = child.kill().await;
    }
    *child_slot = None;
    *state.stdin.lock().await = None;

    let mut status = state.status.lock().await;
    status.running = false;
    Ok(())
}

pub async fn current_status(state: ComputeNodeState) -> OperatorStatus {
    state.status.lock().await.clone()
}

#[cfg(test)]
mod tests {
    use super::build_bridge_command;
    use std::path::Path;

    #[test]
    fn builds_python_bridge_command_for_py_scripts() {
        let command = build_bridge_command("/tmp/compute_node_bridge.py");
        let program = Path::new(command.as_std().get_program())
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or_default();
        assert!(program.contains("python"));
    }
}
