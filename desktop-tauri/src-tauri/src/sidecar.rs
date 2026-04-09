use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceRequest {
    pub request_id: String,
    pub model_path: String,
    pub prompt: String,
    pub mode: ComputeMode,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type")]
pub enum SidecarEvent {
    #[serde(rename = "started")]
    Started,
    #[serde(rename = "token")]
    Token { text: String },
    #[serde(rename = "done")]
    Done,
    #[serde(rename = "canceled")]
    Canceled,
    #[serde(rename = "error")]
    Error { code: String, message: String },
}

#[derive(Debug, Clone, Serialize)]
pub struct UiInferenceEvent {
    pub request_id: String,
    #[serde(flatten)]
    pub event: SidecarEvent,
}

#[derive(Clone, Default)]
pub struct SidecarState {
    pub child: Arc<Mutex<Option<Child>>>,
    pub stdin: Arc<Mutex<Option<ChildStdin>>>,
}

pub fn parse_event_line(line: &str) -> Result<SidecarEvent, serde_json::Error> {
    serde_json::from_str::<SidecarEvent>(line)
}

pub async fn collect_events_from_stdout<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
) -> anyhow::Result<Vec<SidecarEvent>> {
    let mut events = Vec::new();
    let mut lines = BufReader::new(reader).lines();
    while let Some(line) = lines.next_line().await? {
        if let Ok(event) = parse_event_line(&line) {
            events.push(event);
        }
    }
    Ok(events)
}

fn build_sidecar_command(sidecar_path: &str) -> Command {
    let path = Path::new(sidecar_path);
    let is_python = path
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("py"));

    if is_python {
        let python_bin =
            std::env::var("TOKEN_PLACE_SIDECAR_PYTHON").unwrap_or_else(|_| "python".into());
        let mut cmd = Command::new(python_bin);
        cmd.arg(sidecar_path);
        return cmd;
    }

    Command::new(sidecar_path)
}

fn find_existing_inference_bridge_path() -> Option<PathBuf> {
    let mut candidates = Vec::new();

    if let Ok(current_exe) = std::env::current_exe() {
        if let Some(exe_dir) = current_exe.parent() {
            candidates.push(exe_dir.join("python").join("inference_bridge.py"));
            candidates.push(
                exe_dir
                    .join("resources")
                    .join("python")
                    .join("inference_bridge.py"),
            );
            candidates.push(
                exe_dir
                    .join("..")
                    .join("Resources")
                    .join("python")
                    .join("inference_bridge.py"),
            );
        }
    }

    candidates.push(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("python")
            .join("inference_bridge.py"),
    );

    candidates.into_iter().find(|path| path.is_file())
}

fn default_sidecar_script() -> String {
    if let Ok(sidecar) = std::env::var("TOKEN_PLACE_SIDECAR") {
        return sidecar;
    }

    if std::env::var("TOKEN_PLACE_USE_FAKE_SIDECAR")
        .ok()
        .as_deref()
        == Some("1")
    {
        return "../sidecar/fake_llama_sidecar.py".into();
    }

    if let Some(path) = find_existing_inference_bridge_path() {
        return path.to_string_lossy().to_string();
    }

    "../sidecar/fake_llama_sidecar.py".into()
}

pub async fn start_sidecar(
    app: AppHandle,
    state: SidecarState,
    request: InferenceRequest,
) -> anyhow::Result<()> {
    {
        let mut child_slot = state.child.lock().await;
        if child_slot
            .as_mut()
            .is_some_and(|child| child.try_wait().ok().flatten().is_none())
        {
            anyhow::bail!("inference already running; cancel before starting a new request");
        }
        *child_slot = None;
        *state.stdin.lock().await = None;
    }

    let sidecar_script = default_sidecar_script();

    let mut child = build_sidecar_command(&sidecar_script)
        .arg("--model")
        .arg(&request.model_path)
        .arg("--mode")
        .arg(format!("{:?}", request.mode).to_lowercase())
        .arg("--prompt")
        .arg(&request.prompt)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing sidecar stdout"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing sidecar stdin"))?;

    {
        let mut child_slot = state.child.lock().await;
        *child_slot = Some(child);
        let mut stdin_slot = state.stdin.lock().await;
        *stdin_slot = Some(stdin);
    }

    let request_id = request.request_id;
    let mut reader = BufReader::new(stdout).lines();
    while let Some(line) = reader.next_line().await? {
        if let Ok(event) = parse_event_line(&line) {
            app.emit(
                "inference_event",
                UiInferenceEvent {
                    request_id: request_id.clone(),
                    event,
                },
            )?;
        }
    }

    *state.child.lock().await = None;
    *state.stdin.lock().await = None;
    Ok(())
}

pub async fn cancel_sidecar(state: SidecarState) -> anyhow::Result<()> {
    if let Some(stdin) = state.stdin.lock().await.as_mut() {
        stdin.write_all(b"{\"type\":\"cancel\"}\n").await?;
        stdin.flush().await?;
    }

    let mut child_lock = state.child.lock().await;
    if let Some(child) = child_lock.as_mut() {
        for _ in 0..10 {
            if child.try_wait()?.is_some() {
                *child_lock = None;
                *state.stdin.lock().await = None;
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        let _ = child.kill().await;
    }

    *child_lock = None;
    *state.stdin.lock().await = None;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use tempfile::NamedTempFile;
    use tokio::process::Command;

    #[test]
    fn parses_token_event() {
        let event = parse_event_line(r#"{"type":"token","text":"hi"}"#).expect("parse");
        assert_eq!(event, SidecarEvent::Token { text: "hi".into() });
    }

    #[test]
    fn maps_error_event() {
        let event = parse_event_line(r#"{"type":"error","code":"bad_model","message":"no file"}"#)
            .expect("parse");
        assert_eq!(
            event,
            SidecarEvent::Error {
                code: "bad_model".into(),
                message: "no file".into()
            }
        );
    }

    #[tokio::test]
    async fn fake_sidecar_happy_path() {
        let model = NamedTempFile::new().expect("tempfile");
        let mut child = Command::new("python3")
            .arg("../sidecar/fake_llama_sidecar.py")
            .arg("--model")
            .arg(model.path())
            .arg("--mode")
            .arg("cpu")
            .arg("--prompt")
            .arg("hello world")
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn fake sidecar");

        let stdout = child.stdout.take().expect("stdout");
        let events = collect_events_from_stdout(stdout)
            .await
            .expect("collect events");
        assert!(events.iter().any(|e| matches!(e, SidecarEvent::Started)));
        assert!(events.iter().any(|e| matches!(e, SidecarEvent::Done)));
    }

    #[tokio::test]
    async fn real_bridge_happy_path_with_mock_runtime() {
        let mut model = NamedTempFile::new().expect("tempfile");
        model.write_all(b"not-a-real-model").expect("write model");
        let mut child = Command::new("python3")
            .arg("../python/inference_bridge.py")
            .arg("--model")
            .arg(model.path())
            .arg("--mode")
            .arg("cpu")
            .arg("--prompt")
            .arg("hello world")
            .env("USE_MOCK_LLM", "1")
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn inference bridge");

        let stdout = child.stdout.take().expect("stdout");
        let events = collect_events_from_stdout(stdout)
            .await
            .expect("collect events");

        assert!(events.iter().any(|e| matches!(e, SidecarEvent::Started)));
        assert!(events
            .iter()
            .any(|e| matches!(e, SidecarEvent::Token { .. })));
        assert!(events.iter().any(|e| matches!(e, SidecarEvent::Done)));
    }

    #[test]
    fn respects_fake_sidecar_override() {
        let key = "TOKEN_PLACE_USE_FAKE_SIDECAR";
        std::env::set_var(key, "1");
        let selected = default_sidecar_script();
        std::env::remove_var(key);
        assert_eq!(selected, "../sidecar/fake_llama_sidecar.py");
    }
}
