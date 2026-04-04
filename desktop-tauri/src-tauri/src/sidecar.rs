use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum SidecarEvent {
    Started,
    Token { text: String },
    Done,
    Canceled,
    Error { code: String, message: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceRequest {
    pub model_path: String,
    pub prompt: String,
    pub backend: String,
}

pub fn parse_event(line: &str) -> Result<SidecarEvent> {
    let parsed: SidecarEvent = serde_json::from_str(line)
        .map_err(|e| anyhow!("invalid sidecar event: {e}"))?;
    Ok(parsed)
}

pub fn sanitize_log_line(line: &str) -> String {
    if line.contains("prompt=") || line.contains("response=") {
        "[redacted sidecar line]".to_string()
    } else {
        line.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_token_event() {
        let event = parse_event(r#"{"type":"token","text":"hello"}"#).expect("parse");
        assert_eq!(event, SidecarEvent::Token { text: "hello".to_string() });
    }

    #[test]
    fn rejects_invalid_event() {
        assert!(parse_event("not-json").is_err());
    }

    #[test]
    fn redacts_plaintext_markers() {
        let out = sanitize_log_line("prompt=secret");
        assert_eq!(out, "[redacted sidecar line]");
    }
}
