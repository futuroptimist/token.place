use std::collections::BTreeMap;

#[derive(Debug, Clone, Copy)]
pub struct SubprocessLogPolicy {
    pub verbose_raw: bool,
}

impl SubprocessLogPolicy {
    pub fn from_env() -> Self {
        Self {
            verbose_raw: matches!(
                std::env::var("TOKEN_PLACE_VERBOSE_SUBPROCESS_LOGS").as_deref(),
                Ok("1")
            ) || matches!(
                std::env::var("TOKEN_PLACE_VERBOSE_LLM_LOGS").as_deref(),
                Ok("1")
            ),
        }
    }
}

pub struct SubprocessLogFilter {
    source: &'static str,
    request_id: Option<String>,
    policy: SubprocessLogPolicy,
    suppressed_counts: BTreeMap<&'static str, usize>,
    suppressed_total: usize,
}

impl SubprocessLogFilter {
    pub fn new(source: &'static str, policy: SubprocessLogPolicy) -> Self {
        Self {
            source,
            request_id: None,
            policy,
            suppressed_counts: BTreeMap::new(),
            suppressed_total: 0,
        }
    }

    pub fn with_request_id(mut self, request_id: impl Into<String>) -> Self {
        self.request_id = Some(request_id.into());
        self
    }

    pub fn should_emit(&mut self, line: &str) -> bool {
        if self.policy.verbose_raw {
            return true;
        }

        if let Some(pattern) = noisy_pattern(line) {
            if looks_actionable(line) {
                return true;
            }
            self.suppressed_total += 1;
            *self.suppressed_counts.entry(pattern).or_default() += 1;
            return false;
        }

        true
    }

    pub fn flush_summary(&self) {
        if self.suppressed_total == 0 {
            return;
        }
        let breakdown = self
            .suppressed_counts
            .iter()
            .map(|(pattern, count)| format!("{pattern}:{count}"))
            .collect::<Vec<_>>()
            .join(",");
        match &self.request_id {
            Some(request_id) => eprintln!(
                "desktop.subprocess.stderr_summary source={} request_id={} suppressed_total={} patterns={}",
                self.source, request_id, self.suppressed_total, breakdown
            ),
            None => eprintln!(
                "desktop.subprocess.stderr_summary source={} suppressed_total={} patterns={}",
                self.source, self.suppressed_total, breakdown
            ),
        }
    }
}

impl Drop for SubprocessLogFilter {
    fn drop(&mut self) {
        self.flush_summary();
    }
}

fn looks_actionable(line: &str) -> bool {
    let normalized = line.to_ascii_lowercase();
    [
        "error",
        "warning",
        "warn",
        "traceback",
        "exception",
        "failed",
        "failure",
        "fallback",
    ]
    .iter()
    .any(|needle| normalized.contains(needle))
}

fn noisy_pattern(line: &str) -> Option<&'static str> {
    if line.contains("llama_model_loader: Dumping metadata keys/values") {
        return Some("metadata_dump");
    }
    if line.contains("is not marked as EOG") {
        return Some("control_tokens");
    }
    if line.contains("load_tensors: layer") {
        return Some("layer_assignment");
    }
    if line.contains("repack:") {
        return Some("tensor_repack");
    }
    if line.contains("llama_model_loader:")
        && (line.contains("- kv") || line.contains("- type") || line.contains("- name"))
    {
        return Some("metadata_kv");
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn suppresses_known_noisy_patterns_in_default_mode() {
        let policy = SubprocessLogPolicy { verbose_raw: false };
        let mut filter = SubprocessLogFilter::new("test", policy);
        assert!(!filter.should_emit("llama_model_loader: Dumping metadata keys/values"));
        assert!(!filter.should_emit("token '</s>' is not marked as EOG"));
        assert!(!filter.should_emit("load_tensors: layer 1 assigned to CPU"));
        assert!(!filter.should_emit("repack: f16 -> q8_0"));
        assert_eq!(filter.suppressed_total, 4);
    }

    #[test]
    fn keeps_actionable_lines_even_if_noisy_substrings_exist() {
        let policy = SubprocessLogPolicy { verbose_raw: false };
        let mut filter = SubprocessLogFilter::new("test", policy);
        assert!(filter.should_emit("WARNING llama_model_loader: Dumping metadata keys/values"));
        assert!(filter.should_emit("fallback reason: gpu init failed"));
    }

    #[test]
    fn allows_all_lines_when_verbose_is_enabled() {
        let policy = SubprocessLogPolicy { verbose_raw: true };
        let mut filter = SubprocessLogFilter::new("test", policy);
        assert!(filter.should_emit("llama_model_loader: Dumping metadata keys/values"));
    }
}
