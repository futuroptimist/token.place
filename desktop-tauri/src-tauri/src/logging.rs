use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct RedactedInferenceLog {
    pub request_id: String,
    pub prompt_bytes: usize,
    pub output_bytes: usize,
    pub mode: String,
}

pub fn redact_log(
    request_id: &str,
    prompt: &str,
    output: &str,
    mode: &str,
) -> RedactedInferenceLog {
    RedactedInferenceLog {
        request_id: request_id.to_string(),
        prompt_bytes: prompt.len(),
        output_bytes: output.len(),
        mode: mode.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn log_payload_excludes_plaintext() {
        let prompt = "top secret prompt";
        let output = "top secret output";
        let redacted = redact_log("req-1", prompt, output, "cpu");
        let json = serde_json::to_string(&redacted).expect("serialize log");
        assert!(!json.contains(prompt));
        assert!(!json.contains(output));
        assert!(json.contains("prompt_bytes"));
    }
}
