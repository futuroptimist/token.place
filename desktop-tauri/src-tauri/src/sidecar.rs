use std::{path::PathBuf, process::Stdio, sync::Arc};

use serde::{Deserialize, Serialize};
use tauri::{Emitter, WebviewWindow};
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::{Child, Command},
    sync::Mutex,
};

use crate::backend::ComputeMode;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InferenceRequest {
    pub model_path: String,
    pub prompt: String,
    pub preferred_mode: ComputeMode,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
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

#[derive(Default)]
pub struct SidecarManager {
    child: Option<Arc<Mutex<Child>>>,
}

impl SidecarManager {
    pub async fn start(
        &mut self,
        window: WebviewWindow,
        request: InferenceRequest,
    ) -> Result<(), Box<dyn std::error::Error>> {
        if self.child.is_some() {
            return Err("Inference already running".into());
        }

        let script = sidecar_script_path()?;
        let mut cmd = Command::new("python3");
        cmd.arg(script)
            .arg("--model")
            .arg(&request.model_path)
            .arg("--mode")
            .arg(format!("{:?}", request.preferred_mode).to_lowercase())
            .arg("--prompt")
            .arg(&request.prompt)
            .stdin(Stdio::null())
            .stderr(Stdio::piped())
            .stdout(Stdio::piped());

        let mut child = cmd.spawn()?;
        let stdout = child.stdout.take().ok_or("missing sidecar stdout")?;
        let stderr = child.stderr.take().ok_or("missing sidecar stderr")?;

        let child_arc = Arc::new(Mutex::new(child));
        self.child = Some(child_arc.clone());

        let window_for_stdout = window.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                match parse_sidecar_event(&line) {
                    Ok(event) => {
                        if window_for_stdout.emit("inference-event", event).is_err() {
                            break;
                        }
                    }
                    Err(_) => {
                        let _ = window_for_stdout.emit(
                            "inference-event",
                            SidecarEvent::Error {
                                code: String::from("bad_event"),
                                message: String::from("Sidecar emitted malformed event"),
                            },
                        );
                    }
                }
            }
        });

        let window_for_stderr = window.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stderr).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if !line.trim().is_empty() {
                    let _ = window_for_stderr.emit(
                        "inference-event",
                        SidecarEvent::Error {
                            code: String::from("sidecar_stderr"),
                            message: redact_log_line(&line),
                        },
                    );
                }
            }
        });

        let window_for_exit = window;
        tokio::spawn(async move {
            let mut locked = child_arc.lock().await;
            let _ = locked.wait().await;
            let _ = window_for_exit.emit("inference-event", SidecarEvent::Done);
        });

        Ok(())
    }

    pub async fn cancel(&mut self) -> Result<(), Box<dyn std::error::Error>> {
        if let Some(child) = self.child.take() {
            let mut locked = child.lock().await;
            let _ = locked.kill().await;
        }
        Ok(())
    }
}

fn sidecar_script_path() -> Result<PathBuf, Box<dyn std::error::Error>> {
    let cwd = std::env::current_dir()?;
    Ok(cwd.join("desktop-tauri/sidecar/mock_llama_sidecar.py"))
}

pub fn redact_log_line(line: &str) -> String {
    let lowercase = line.to_lowercase();
    if lowercase.contains("prompt") || lowercase.contains("response") || lowercase.contains("token")
    {
        return String::from("[redacted sidecar diagnostic]");
    }
    line.to_string()
}

pub fn parse_sidecar_event(line: &str) -> Result<SidecarEvent, serde_json::Error> {
    serde_json::from_str(line)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_token_event() {
        let event = parse_sidecar_event(r#"{"type":"token","text":"hi"}"#).unwrap();
        assert_eq!(
            event,
            SidecarEvent::Token {
                text: String::from("hi")
            }
        );
    }

    #[test]
    fn redacts_plaintexty_logs() {
        assert_eq!(
            redact_log_line("prompt=secret text"),
            "[redacted sidecar diagnostic]"
        );
    }
}
