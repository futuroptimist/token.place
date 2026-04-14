use serde::Serialize;
use std::collections::BTreeMap;

const DEFAULT_VERBOSE_LLAMA_ENV: &str = "TOKEN_PLACE_VERBOSE_LLAMA_LOGS";

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum NoiseKind {
    MetadataDump,
    ControlTokenSpam,
    LayerAssignmentSpam,
    RepackSpam,
    InitChatter,
}

impl NoiseKind {
    fn label(self) -> &'static str {
        match self {
            NoiseKind::MetadataDump => "metadata_dump",
            NoiseKind::ControlTokenSpam => "control_token_spam",
            NoiseKind::LayerAssignmentSpam => "layer_assignment_spam",
            NoiseKind::RepackSpam => "tensor_repack_spam",
            NoiseKind::InitChatter => "llama_init_chatter",
        }
    }
}

#[derive(Debug, Default, Clone)]
pub struct SubprocessLogFilterSummary {
    pub shown_lines: usize,
    pub suppressed_lines: usize,
    pub suppressed_by_kind: BTreeMap<String, usize>,
}

#[derive(Debug, Clone)]
pub struct SubprocessLogFilter {
    verbose: bool,
    summary: SubprocessLogFilterSummary,
}

impl Default for SubprocessLogFilter {
    fn default() -> Self {
        Self::new_from_env(DEFAULT_VERBOSE_LLAMA_ENV)
    }
}

impl SubprocessLogFilter {
    pub fn new_from_env(verbose_env_key: &str) -> Self {
        Self {
            verbose: std::env::var(verbose_env_key).is_ok_and(|v| v.trim() == "1"),
            summary: SubprocessLogFilterSummary::default(),
        }
    }

    pub fn is_verbose(&self) -> bool {
        self.verbose
    }

    pub fn classify_and_filter<'a>(&mut self, raw_line: &'a str) -> Option<&'a str> {
        let line = raw_line.trim();
        if line.is_empty() {
            return None;
        }
        if self.verbose || is_important_runtime_line(line) {
            self.summary.shown_lines += 1;
            return Some(line);
        }
        if let Some(kind) = classify_noise_line(line) {
            self.summary.suppressed_lines += 1;
            *self
                .summary
                .suppressed_by_kind
                .entry(kind.label().to_string())
                .or_insert(0) += 1;
            return None;
        }

        self.summary.shown_lines += 1;
        Some(line)
    }

    pub fn finish(self) -> SubprocessLogFilterSummary {
        self.summary
    }
}

fn is_important_runtime_line(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    [
        "error",
        "warning",
        "warn:",
        "failed",
        "exception",
        "traceback",
        "fatal",
        "panic",
    ]
    .iter()
    .any(|needle| lower.contains(needle))
}

fn classify_noise_line(line: &str) -> Option<NoiseKind> {
    if line.contains("llama_model_loader: Dumping metadata keys/values") {
        return Some(NoiseKind::MetadataDump);
    }
    if line.contains("is not marked as EOG") {
        return Some(NoiseKind::ControlTokenSpam);
    }
    if line.contains("load_tensors: layer")
        || line.contains("offloading layer")
        || line.contains("offloaded")
    {
        return Some(NoiseKind::LayerAssignmentSpam);
    }
    if line.contains("repack:") {
        return Some(NoiseKind::RepackSpam);
    }
    if line.starts_with("llama_kv_cache")
        || line.starts_with("llama_context:")
        || line.starts_with("llama_model_loader:")
        || line.starts_with("llama_new_context_with_model")
    {
        return Some(NoiseKind::InitChatter);
    }
    None
}

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

    #[test]
    fn subprocess_filter_hides_expected_llama_noise_by_default() {
        let mut filter = SubprocessLogFilter::new_from_env("TOKEN_PLACE_VERBOSE_LLAMA_LOGS_TEST");
        assert!(
            filter
                .classify_and_filter(
                    "llama_model_loader: Dumping metadata keys/values. Note: KV overrides do not apply in this output."
                )
                .is_none()
        );
        assert!(filter
            .classify_and_filter(
                "llama_model_loader: control token: 128009 '<|eot_id|>' is not marked as EOG"
            )
            .is_none());
        assert!(filter
            .classify_and_filter("llama_model_load_tensors: layer 12 assigned to CPU")
            .is_none());
        assert!(filter
            .classify_and_filter("llama_log_callback: warning: using fallback implementation")
            .is_some());
        let summary = filter.finish();
        assert_eq!(summary.shown_lines, 1);
        assert_eq!(summary.suppressed_lines, 3);
    }
}
