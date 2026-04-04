use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use std::process::Stdio;
use std::sync::Arc;
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

pub async fn start_sidecar(
    app: AppHandle,
    state: SidecarState,
    request: InferenceRequest,
) -> anyhow::Result<()> {
    let sidecar_script = std::env::var("TOKEN_PLACE_SIDECAR")
        .unwrap_or_else(|_| "../sidecar/fake_llama_sidecar.py".into());

    let mut child = Command::new("python3")
        .arg(sidecar_script)
        .arg("--model")
        .arg(&request.model_path)
        .arg("--mode")
        .arg(format!("{:?}", request.mode).to_lowercase())
        .arg("--prompt")
        .arg(&request.prompt)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
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

    Ok(())
}

pub async fn cancel_sidecar(state: SidecarState) -> anyhow::Result<()> {
    if let Some(stdin) = state.stdin.lock().await.as_mut() {
        stdin.write_all(b"{\"type\":\"cancel\"}\n").await?;
    }
    if let Some(child) = state.child.lock().await.as_mut() {
        let _ = child.kill().await;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
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
        let mut child = Command::new("python3")
            .arg("../sidecar/fake_llama_sidecar.py")
            .arg("--model")
            .arg("/tmp/model.gguf")
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
}
