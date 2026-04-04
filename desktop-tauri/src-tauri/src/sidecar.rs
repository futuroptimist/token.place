use std::process::Stdio;

use serde::{Deserialize, Serialize};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, Command},
    sync::mpsc,
};
use uuid::Uuid;

use crate::{backend::detect_backend, logging::redact_text_for_log};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct InferenceEvent {
    pub run_id: String,
    #[serde(rename = "type")]
    pub event_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

#[derive(Default)]
pub struct SidecarManager {
    tx: Option<mpsc::Sender<InferenceEvent>>,
    rx: Option<mpsc::Receiver<InferenceEvent>>,
    child: Option<Child>,
    active_run_id: Option<String>,
}

impl SidecarManager {
    pub async fn start(
        &mut self,
        model_path: String,
        prompt: String,
        compute_mode: String,
    ) -> anyhow::Result<String> {
        self.cancel_active().await?;
        let (tx, rx) = mpsc::channel(128);
        self.tx = Some(tx.clone());
        self.rx = Some(rx);

        let detected = detect_backend(std::env::consts::OS, std::env::consts::ARCH);
        let backend = if compute_mode == "cpu" {
            "cpu".to_string()
        } else {
            detected.backend.to_string()
        };

        let run_id = Uuid::new_v4().to_string();
        self.active_run_id = Some(run_id.clone());

        let mut child = spawn_sidecar(&model_path, &backend).await?;
        let mut stdin = child
            .stdin
            .take()
            .ok_or_else(|| anyhow::anyhow!("sidecar stdin unavailable"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| anyhow::anyhow!("sidecar stdout unavailable"))?;

        let payload = serde_json::json!({ "type": "infer", "prompt": prompt });
        stdin.write_all(format!("{}\n", payload).as_bytes()).await?;
        stdin.flush().await?;

        let run_id_for_reader = run_id.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(stdout).lines();
            let _ = tx
                .send(InferenceEvent {
                    run_id: run_id_for_reader.clone(),
                    event_type: "started".to_string(),
                    text: None,
                    code: None,
                    message: None,
                })
                .await;

            while let Ok(Some(line)) = lines.next_line().await {
                if let Some(evt) = parse_sidecar_line(&run_id_for_reader, &line) {
                    let _ = tx.send(evt).await;
                }
            }
        });

        eprintln!(
            "token.place sidecar started model={} prompt={} backend={}",
            model_path,
            redact_text_for_log(&prompt),
            backend
        );

        self.child = Some(child);
        Ok(run_id)
    }

    pub fn subscribe(&mut self) -> Option<mpsc::Receiver<InferenceEvent>> {
        self.rx.take()
    }

    pub async fn cancel(&mut self, run_id: &str) -> anyhow::Result<()> {
        if self.active_run_id.as_deref() != Some(run_id) {
            return Ok(());
        }
        self.cancel_active().await?;
        if let Some(tx) = &self.tx {
            let _ = tx
                .send(InferenceEvent {
                    run_id: run_id.to_string(),
                    event_type: "canceled".to_string(),
                    text: None,
                    code: None,
                    message: None,
                })
                .await;
        }
        Ok(())
    }

    async fn cancel_active(&mut self) -> anyhow::Result<()> {
        if let Some(child) = &mut self.child {
            let _ = child.kill().await;
        }
        self.child = None;
        self.active_run_id = None;
        Ok(())
    }
}

async fn spawn_sidecar(model_path: &str, backend: &str) -> anyhow::Result<Child> {
    let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("sidecar")
        .join("fake_llama_sidecar.py");

    let child = Command::new("python3")
        .arg(root)
        .arg("--model")
        .arg(model_path)
        .arg("--backend")
        .arg(backend)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    Ok(child)
}

pub fn parse_sidecar_line(run_id: &str, line: &str) -> Option<InferenceEvent> {
    #[derive(Deserialize)]
    struct RawEvent {
        #[serde(rename = "type")]
        event_type: String,
        text: Option<String>,
        code: Option<String>,
        message: Option<String>,
    }

    let parsed = serde_json::from_str::<RawEvent>(line).ok()?;
    let kind = parsed.event_type;
    let supported = ["started", "token", "done", "error"];
    if !supported.contains(&kind.as_str()) {
        return None;
    }
    Some(InferenceEvent {
        run_id: run_id.to_string(),
        event_type: kind,
        text: parsed.text,
        code: parsed.code,
        message: parsed.message,
    })
}

#[cfg(test)]
mod tests {
    use super::{parse_sidecar_line, SidecarManager};

    #[test]
    fn parses_token_event() {
        let event = parse_sidecar_line("run-1", r#"{"type":"token","text":"hello"}"#).unwrap();
        assert_eq!(event.event_type, "token");
        assert_eq!(event.text.as_deref(), Some("hello"));
    }

    #[test]
    fn ignores_unknown_event_types() {
        let event = parse_sidecar_line("run-1", r#"{"type":"progress","pct":10}"#);
        assert!(event.is_none());
    }

    #[tokio::test]
    async fn sidecar_manager_streams_mocked_output() {
        let mut manager = SidecarManager::default();
        let run = manager
            .start(
                "model.gguf".to_string(),
                "hello world".to_string(),
                "auto".to_string(),
            )
            .await
            .unwrap();
        let mut rx = manager.subscribe().unwrap();
        let mut saw_done = false;
        while let Some(evt) = rx.recv().await {
            if evt.run_id == run && evt.event_type == "done" {
                saw_done = true;
                break;
            }
        }
        assert!(saw_done);
    }
}
