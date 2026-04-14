use std::collections::BTreeMap;

#[derive(Debug, Clone)]
pub enum StderrDecision {
    Emit(String),
    Suppressed,
}

#[derive(Debug, Default, Clone)]
pub struct StderrFilter {
    label: &'static str,
    context: String,
    suppressed_total: usize,
    suppressed_by_pattern: BTreeMap<&'static str, usize>,
}

impl StderrFilter {
    pub fn new(label: &'static str, context: impl Into<String>) -> Self {
        Self {
            label,
            context: context.into(),
            ..Self::default()
        }
    }

    pub fn classify(&mut self, line: &str) -> StderrDecision {
        if is_high_priority(line) {
            return StderrDecision::Emit(format!(
                "desktop.{}.stderr.important {} line={}",
                self.label, self.context, line
            ));
        }

        if let Some(pattern_name) = noisy_pattern_name(line) {
            self.suppressed_total += 1;
            *self.suppressed_by_pattern.entry(pattern_name).or_insert(0) += 1;
            return StderrDecision::Suppressed;
        }

        if is_useful_summary_line(line) {
            return StderrDecision::Emit(format!(
                "desktop.{}.stderr {} line={}",
                self.label, self.context, line
            ));
        }

        self.suppressed_total += 1;
        *self
            .suppressed_by_pattern
            .entry("other_low_level")
            .or_insert(0) += 1;
        StderrDecision::Suppressed
    }

    pub fn flush_summary(&self) -> Option<String> {
        if self.suppressed_total == 0 {
            return None;
        }

        let mut fragments = Vec::new();
        for (pattern, count) in &self.suppressed_by_pattern {
            fragments.push(format!("{}={}", pattern, count));
        }

        Some(format!(
            "desktop.{}.stderr.filtered {} total={} details={} hint=Set TOKEN_PLACE_DESKTOP_VERBOSE_LOGS=1 for raw llama.cpp stderr",
            self.label,
            self.context,
            self.suppressed_total,
            fragments.join(",")
        ))
    }
}

pub fn verbose_stderr_enabled() -> bool {
    matches!(
        std::env::var("TOKEN_PLACE_DESKTOP_VERBOSE_LOGS").as_deref(),
        Ok("1") | Ok("true") | Ok("TRUE") | Ok("yes") | Ok("on")
    )
}

fn noisy_pattern_name(line: &str) -> Option<&'static str> {
    let normalized = line.trim();
    if normalized.contains("llama_model_loader: Dumping metadata keys/values") {
        return Some("metadata_dump");
    }
    if normalized.contains("is not marked as EOG") {
        return Some("control_token_spam");
    }
    if normalized.contains("load_tensors: layer") {
        return Some("layer_assignment");
    }
    if normalized.contains("repack:") {
        return Some("tensor_repack");
    }
    if normalized.contains("llm_load_print_meta:")
        || normalized.contains("llama_context:")
        || normalized.contains("llama_new_context_with_model:")
        || normalized.contains("llama_kv_cache_unified:")
        || normalized.contains("llama_init_from_model:")
    {
        return Some("init_chatter");
    }
    None
}

fn is_high_priority(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    lower.contains("error")
        || lower.contains("warn")
        || lower.contains("traceback")
        || lower.contains("exception")
        || lower.contains("failed")
}

fn is_useful_summary_line(line: &str) -> bool {
    line.contains("llama_perf_context_print")
        || line.contains("prompt eval time")
        || line.contains("eval time")
        || line.contains("load time")
        || line.contains("sample time")
}
