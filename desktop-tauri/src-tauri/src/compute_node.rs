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
pub struct ComputeNodeStatus {
    pub state: String,
    pub registered: bool,
    pub running: bool,
    pub relay_url: String,
    pub backend_mode: String,
    pub model_path: String,
    pub last_error: String,
}

impl Default for ComputeNodeStatus {
    fn default() -> Self {
        Self {
            state: "stopped".into(),
            registered: false,
            running: false,
            relay_url: String::new(),
            backend_mode: "unknown".into(),
            model_path: String::new(),
            last_error: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComputeNodeStartRequest {
    pub model_path: String,
    pub relay_base_url: String,
    pub preferred_mode: ComputeMode,
}

#[derive(Clone, Default)]
pub struct ComputeNodeState {
    child: Arc<Mutex<Option<Child>>>,
    stdin: Arc<Mutex<Option<ChildStdin>>>,
    latest_status: Arc<Mutex<ComputeNodeStatus>>,
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

    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("python")
        .join("compute_node_bridge.py")
        .to_string_lossy()
        .into_owned()
}

fn build_bridge_command(script: &str) -> Command {
    let python_bin = std::env::var("TOKEN_PLACE_COMPUTE_NODE_PYTHON")
        .or_else(|_| std::env::var("TOKEN_PLACE_SIDECAR_PYTHON"))
        .unwrap_or_else(|_| "python3".into());

    let mut cmd = Command::new(python_bin);
    cmd.arg(script);
    cmd
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

    let script = resolve_bridge_script();
    let mut child = build_bridge_command(&script)
        .arg("--model")
        .arg(&request.model_path)
        .arg("--relay-url")
        .arg(&request.relay_base_url)
        .arg("--mode")
        .arg(format!("{:?}", request.preferred_mode).to_lowercase())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute node bridge stdout"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute node bridge stdin"))?;

    {
        let mut child_slot = state.child.lock().await;
        *child_slot = Some(child);
        *state.stdin.lock().await = Some(stdin);
    }

    let state_clone = state.clone();
    tokio::spawn(async move {
        let mut lines = BufReader::new(stdout).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let parsed = serde_json::from_str::<serde_json::Value>(&line);
            if let Ok(value) = parsed {
                if value.get("type").and_then(|v| v.as_str()) == Some("status") {
                    if let Ok(status) = serde_json::from_value::<ComputeNodeStatus>(value.clone()) {
                        *state_clone.latest_status.lock().await = status.clone();
                        let _ = app.emit("compute_node_status", status);
                    }
                }
            }
        }

        state_clone.child.lock().await.take();
        state_clone.stdin.lock().await.take();
    });

    Ok(())
}

pub async fn stop_compute_node(state: ComputeNodeState) -> anyhow::Result<()> {
    if let Some(stdin) = state.stdin.lock().await.as_mut() {
        stdin.write_all(b"{\"type\":\"stop\"}\n").await?;
        stdin.flush().await?;
    }

    if let Some(child) = state.child.lock().await.as_mut() {
        let _ = tokio::time::timeout(std::time::Duration::from_secs(2), child.wait()).await;
        if child.try_wait()?.is_none() {
            let _ = child.kill().await;
        }
    }

    state.child.lock().await.take();
    state.stdin.lock().await.take();

    let mut status = state.latest_status.lock().await;
    status.running = false;
    status.registered = false;
    if status.state != "failed" {
        status.state = "stopped".into();
    }

    Ok(())
}

pub async fn latest_status(state: ComputeNodeState) -> ComputeNodeStatus {
    state.latest_status.lock().await.clone()
}

#[cfg(test)]
mod tests {
    use super::resolve_bridge_script;

    #[test]
    fn resolves_compute_node_bridge_script_path() {
        let script = resolve_bridge_script();
        assert!(script.ends_with("compute_node_bridge.py"));
    }
}
