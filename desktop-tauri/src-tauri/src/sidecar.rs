use crate::backend::ComputeMode;
use crate::python_runtime::command_for_python_script;
use serde::{Deserialize, Serialize};
use std::path::Path;
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

async fn drain_sidecar_stderr<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
    request_id: &str,
) -> anyhow::Result<()> {
    let mut lines = BufReader::new(reader).lines();
    while let Some(line) = lines.next_line().await? {
        eprintln!("desktop.sidecar.stderr request_id={request_id} line={line}");
    }
    Ok(())
}

fn build_sidecar_command(sidecar_path: &str) -> anyhow::Result<Command> {
    let path = Path::new(sidecar_path);
    let is_python = path
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("py"));

    if is_python {
        return command_for_python_script(sidecar_path, "TOKEN_PLACE_SIDECAR_PYTHON");
    }

    Ok(Command::new(sidecar_path))
}

fn resolve_default_sidecar_script() -> String {
    let mut candidates = Vec::new();

    if let Ok(exe_path) = std::env::current_exe() {
        if let Some(exe_dir) = exe_path.parent() {
            candidates.push(exe_dir.join("python").join("inference_sidecar.py"));
            candidates.push(
                exe_dir
                    .join("resources")
                    .join("python")
                    .join("inference_sidecar.py"),
            );
            candidates.push(
                exe_dir
                    .join("resources")
                    .join("python")
                    .join("fake_llama_sidecar.py"),
            );
            candidates.push(exe_dir.join("inference_sidecar.py"));
            candidates.push(exe_dir.join("fake_llama_sidecar.py"));

            if let Some(parent_dir) = exe_dir.parent() {
                candidates.push(
                    parent_dir
                        .join("Resources")
                        .join("python")
                        .join("inference_sidecar.py"),
                );
                candidates.push(
                    parent_dir
                        .join("Resources")
                        .join("python")
                        .join("fake_llama_sidecar.py"),
                );
                candidates.push(
                    parent_dir
                        .join("resources")
                        .join("python")
                        .join("inference_sidecar.py"),
                );
                candidates.push(
                    parent_dir
                        .join("resources")
                        .join("python")
                        .join("fake_llama_sidecar.py"),
                );
            }
        }
    }

    candidates.push(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("python")
            .join("inference_sidecar.py"),
    );
    candidates.push(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("sidecar")
            .join("fake_llama_sidecar.py"),
    );

    for candidate in candidates {
        if candidate.is_file() {
            return candidate.to_string_lossy().into_owned();
        }
    }

    "../sidecar/fake_llama_sidecar.py".into()
}

fn should_force_fake_sidecar() -> bool {
    matches!(
        std::env::var("TOKEN_PLACE_USE_FAKE_SIDECAR").as_deref(),
        Ok("1")
    )
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

    let sidecar_script = std::env::var("TOKEN_PLACE_SIDECAR").unwrap_or_else(|_| {
        if should_force_fake_sidecar() {
            "../sidecar/fake_llama_sidecar.py".into()
        } else {
            resolve_default_sidecar_script()
        }
    });

    let mut sidecar_command = build_sidecar_command(&sidecar_script)?;
    let mut child = sidecar_command
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
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing sidecar stderr"))?;
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
    let stderr_request_id = request_id.clone();
    let stderr_task = tokio::spawn(async move {
        if let Err(err) = drain_sidecar_stderr(stderr, &stderr_request_id).await {
            eprintln!(
                "desktop.sidecar.stderr_error request_id={} error={}",
                stderr_request_id, err
            );
        }
    });

    let mut reader = BufReader::new(stdout).lines();
    let mut saw_error_event = false;
    while let Some(line) = reader.next_line().await? {
        match parse_event_line(&line) {
            Ok(event) => {
                if matches!(event, SidecarEvent::Error { .. }) {
                    saw_error_event = true;
                }
                app.emit(
                    "inference_event",
                    UiInferenceEvent {
                        request_id: request_id.clone(),
                        event,
                    },
                )?;
            }
            Err(err) => {
                eprintln!(
                    "desktop.sidecar.stdout_parse_error request_id={} error={} line={}",
                    request_id, err, line
                );
            }
        }
    }

    if let Err(err) = stderr_task.await {
        eprintln!(
            "desktop.sidecar.stderr_task_join_error request_id={} error={}",
            request_id, err
        );
    }

    let running_child = {
        let mut child_slot = state.child.lock().await;
        child_slot.take()
    };

    if let Some(mut running_child) = running_child {
        let exit_status = running_child.wait().await?;

        if !exit_status.success() && !saw_error_event {
            app.emit(
                "inference_event",
                UiInferenceEvent {
                    request_id: request_id.clone(),
                    event: SidecarEvent::Error {
                        code: "sidecar_exit".into(),
                        message: format!(
                            "sidecar exited with status {exit_status}; see desktop.sidecar.stderr logs"
                        ),
                    },
                },
            )?;
        }
    }

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
            .stdin(Stdio::null())
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
        let model = NamedTempFile::new().expect("tempfile");
        let sidecar_script = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("python")
            .join("inference_sidecar.py");

        let mut child = Command::new("python3")
            .arg(sidecar_script)
            .arg("--model")
            .arg(model.path())
            .arg("--mode")
            .arg("cpu")
            .arg("--prompt")
            .arg("hello world")
            .env("USE_MOCK_LLM", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn bridge sidecar");

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

    #[tokio::test]
    async fn drain_sidecar_stderr_reads_all_lines() {
        let script = NamedTempFile::new().expect("temp script");
        std::fs::write(
            script.path(),
            "#!/usr/bin/env python3\nimport sys\nprint('first', file=sys.stderr)\nprint('second', file=sys.stderr)\n",
        )
        .expect("write script");

        let mut child = Command::new("python3")
            .arg(script.path())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn stderr script");

        let stderr = child.stderr.take().expect("stderr");
        drain_sidecar_stderr(stderr, "test")
            .await
            .expect("drain stderr");
        let status = child.wait().await.expect("wait child");
        assert!(status.success());
    }

    #[tokio::test]
    async fn collect_events_ignores_malformed_stdout_lines_and_keeps_valid_flow() {
        let stdout = b"{\"type\":\"started\"}\nnot-json\n{\"type\":\"token\",\"text\":\"ok\"}\n{\"type\":\"done\"}\n"
            .as_slice();
        let events = collect_events_from_stdout(stdout)
            .await
            .expect("collect events");
        assert_eq!(
            events,
            vec![
                SidecarEvent::Started,
                SidecarEvent::Token { text: "ok".into() },
                SidecarEvent::Done
            ]
        );
    }
}
