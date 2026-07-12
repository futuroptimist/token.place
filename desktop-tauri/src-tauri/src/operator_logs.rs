use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager};

#[derive(Clone)]
pub struct OperatorLogSink {
    pub(crate) path: PathBuf,
    pub(crate) file: Arc<Mutex<File>>,
}

impl OperatorLogSink {
    pub fn create(app: &AppHandle, session_id: &str) -> anyhow::Result<Self> {
        let dir = operator_log_dir(app)?;
        fs::create_dir_all(&dir)?;
        let (path, file) = create_unique_operator_log_file(&dir, session_id)?;
        Ok(Self {
            path,
            file: Arc::new(Mutex::new(file)),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append_line(&self, source: &str, line: &str) {
        let sanitized = sanitize_operator_diagnostic_line(line);
        if let Ok(mut file) = self.file.lock() {
            let _ = writeln!(file, "{} {} {}", current_time_ms(), source, sanitized);
            let _ = file.flush();
        }
    }
}

pub fn operator_log_dir(app: &AppHandle) -> anyhow::Result<PathBuf> {
    let base = app
        .path()
        .app_log_dir()
        .or_else(|_| app.path().app_data_dir().map(|dir| dir.join("logs")))
        .map_err(|err| anyhow::anyhow!("operator log path error: {err}"))?;
    Ok(base.join("operator"))
}

pub fn compute_operator_log_path(dir: &Path, session_id: &str) -> PathBuf {
    let safe_session_id = sanitize_filename_component(session_id);
    dir.join(format!("compute-node-{safe_session_id}.log"))
}

fn create_unique_operator_log_file(
    dir: &Path,
    session_id: &str,
) -> anyhow::Result<(PathBuf, File)> {
    let safe_session_id = sanitize_filename_component(session_id);
    for attempt in 0..100 {
        let timestamp = current_time_ms();
        let suffix = if attempt == 0 {
            String::new()
        } else {
            format!("-{attempt}")
        };
        let path = dir.join(format!(
            "compute-node-{safe_session_id}-{timestamp}{suffix}.log"
        ));
        match OpenOptions::new().append(true).create_new(true).open(&path) {
            Ok(file) => return Ok((path, file)),
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(err.into()),
        }
    }
    anyhow::bail!("failed to create a unique operator log file after 100 attempts")
}

pub fn append_line_to_path(log_path: &Path, source: &str, line: &str) -> anyhow::Result<()> {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)?;
    writeln!(
        file,
        "{} {} {}",
        current_time_ms(),
        source,
        sanitize_operator_diagnostic_line(line)
    )?;
    Ok(())
}

pub fn append_model_bridge_log(
    app: &AppHandle,
    action: &str,
    line: &str,
) -> anyhow::Result<PathBuf> {
    let dir = operator_log_dir(app)?;
    fs::create_dir_all(&dir)?;
    let path = dir.join("model-bridge.log");
    let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
    writeln!(
        file,
        "{} desktop.model_bridge.{} {}",
        current_time_ms(),
        sanitize_filename_component(action),
        sanitize_operator_diagnostic_line(line)
    )?;
    Ok(path)
}

pub fn tail_terminal_script(log_path: &Path) -> String {
    format!(
        "clear; echo 'Tailing token.place operator log:'; echo {}; tail -n 200 -F {}",
        quote_posix_arg(&log_path.display().to_string()),
        quote_posix_arg(&log_path.display().to_string())
    )
}

pub fn quote_posix_arg(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

pub fn open_debug_terminal(log_path: &Path) -> anyhow::Result<()> {
    #[cfg(target_os = "macos")]
    {
        Command::new("osascript")
            .arg("-e")
            .arg(format!(
                "tell application \"Terminal\" to do script {}",
                quote_applescript_string(&tail_terminal_script(log_path))
            ))
            .spawn()?;
        return Ok(());
    }

    #[cfg(target_os = "windows")]
    {
        Command::new("cmd")
            .args([
                "/C",
                "start",
                "token.place operator log",
                "powershell",
                "-NoExit",
                "-Command",
                &format!(
                    "Get-Content -LiteralPath {} -Tail 200 -Wait",
                    quote_powershell_single_string(&log_path.display().to_string())
                ),
            ])
            .spawn()?;
        return Ok(());
    }

    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        Command::new("x-terminal-emulator")
            .args(["-e", "tail", "-n", "200", "-F"])
            .arg(log_path)
            .spawn()?;
        Ok(())
    }
}

pub fn reveal_log_file(log_path: &Path) -> anyhow::Result<()> {
    #[cfg(target_os = "macos")]
    {
        Command::new("open").arg("-R").arg(log_path).spawn()?;
        return Ok(());
    }

    #[cfg(target_os = "windows")]
    {
        Command::new("explorer")
            .arg(format!("/select,{}", log_path.display()))
            .spawn()?;
        return Ok(());
    }

    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    {
        let dir = log_path.parent().unwrap_or_else(|| Path::new("."));
        Command::new("xdg-open").arg(dir).spawn()?;
        Ok(())
    }
}

pub fn read_log_tail(log_path: &Path, max_bytes: usize) -> anyhow::Result<String> {
    let mut file = File::open(log_path)?;
    let len = file.metadata()?.len();
    let start = len.saturating_sub(max_bytes as u64);
    file.seek(SeekFrom::Start(start))?;
    let mut bytes = Vec::with_capacity((len - start) as usize);
    file.read_to_end(&mut bytes)?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

pub fn sanitize_operator_diagnostic_line(line: &str) -> String {
    let line = sanitize_log_line(line);
    let trimmed = line.trim();
    if trimmed.starts_with('{') || trimmed.starts_with('[') {
        if trimmed.len() > 4096 {
            return r#"{"type":"operator_log_json_token_truncated","safe_truncation":true}"#
                .to_string();
        }
        if let Ok(value) = serde_json::from_str::<serde_json::Value>(trimmed) {
            return serde_json::to_string(&sanitize_operator_json_value(&value))
                .unwrap_or_else(|_| r#"{"type":"operator_log_json_sanitize_error"}"#.to_string());
        }
    }
    line.split_whitespace()
        .map(sanitize_operator_diagnostic_token)
        .collect::<Vec<_>>()
        .join(" ")
}

pub fn sanitize_operator_path_display(path: &Path) -> String {
    sanitize_path_display(&path.display().to_string())
}

fn sanitize_operator_diagnostic_token(token: &str) -> String {
    if token.starts_with('{') || token.starts_with('[') {
        if token.len() <= 4096 {
            return token.to_string();
        }
        return r#"{"type":"operator_log_json_token_truncated","safe_truncation":true}"#
            .to_string();
    }
    if token.starts_with("http://") || token.starts_with("https://") {
        return sanitize_url_display(token);
    }

    for separator in ['=', ':'] {
        if let Some((key, value)) = token.split_once(separator) {
            if value.starts_with("http://") || value.starts_with("https://") {
                return format!("{key}{separator}{}", sanitize_url_display(value));
            }
            if is_path_like(value) {
                return format!("{key}{separator}{}", sanitize_path_display(value));
            }
        }
    }

    if is_path_like(token) {
        return sanitize_path_display(token);
    }

    token.chars().take(4096).collect()
}

fn sanitize_operator_json_value(value: &serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::Object(map) => {
            let mut sanitized = serde_json::Map::new();
            for (key, value) in map {
                let normalized = key.to_ascii_lowercase();
                if let Some(safe_token_metadata) =
                    sanitize_safe_token_metadata_value(&normalized, value)
                {
                    sanitized.insert(key.clone(), safe_token_metadata);
                } else if normalized.contains("api_key")
                    || normalized.contains("token")
                    || normalized.contains("prompt")
                    || normalized.contains("output")
                    || normalized.contains("response")
                    || normalized.contains("private_key")
                    || normalized.contains("payload")
                {
                    sanitized.insert(key.clone(), redacted_json_value());
                } else {
                    sanitized.insert(key.clone(), sanitize_operator_json_value(value));
                }
            }
            serde_json::Value::Object(sanitized)
        }
        serde_json::Value::Array(items) => serde_json::Value::Array(
            items
                .iter()
                .take(32)
                .map(sanitize_operator_json_value)
                .collect(),
        ),
        serde_json::Value::String(text) => {
            serde_json::Value::String(sanitize_operator_diagnostic_token(text))
        }
        _ => value.clone(),
    }
}

fn redacted_json_value() -> serde_json::Value {
    serde_json::Value::String("<redacted>".into())
}

fn sanitize_safe_token_metadata_value(
    normalized_key: &str,
    value: &serde_json::Value,
) -> Option<serde_json::Value> {
    let valid = if SAFE_TOKEN_INTEGER_OR_NULL_KEYS.contains(&normalized_key) {
        value.is_null() || value.as_u64().is_some()
    } else if SAFE_TOKEN_BOOL_OR_NULL_KEYS.contains(&normalized_key) {
        value.is_null() || value.as_bool().is_some()
    } else if SAFE_TOKEN_IDENTIFIER_OR_NULL_KEYS.contains(&normalized_key) {
        value.is_null() || value.as_str().is_some_and(is_bounded_safe_token_identifier)
    } else if SAFE_TOKEN_CSV_IDENTIFIER_OR_NULL_KEYS.contains(&normalized_key) {
        value.is_null()
            || value
                .as_str()
                .is_some_and(is_bounded_safe_token_identifier_csv)
    } else if SAFE_TOKEN_CSV_INTEGER_OR_NULL_KEYS.contains(&normalized_key) {
        value.is_null()
            || value
                .as_str()
                .is_some_and(is_bounded_safe_token_integer_csv)
    } else {
        return None;
    };

    Some(if valid {
        value.clone()
    } else {
        redacted_json_value()
    })
}

fn is_bounded_safe_token_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric()
                || matches!(byte, b'_' | b'.' | b':' | b'/' | b'@' | b'+' | b'-')
        })
}

fn is_bounded_safe_token_identifier_csv(value: &str) -> bool {
    validate_bounded_csv(value, is_bounded_safe_token_identifier)
}

fn is_bounded_safe_token_integer_csv(value: &str) -> bool {
    validate_bounded_csv(value, |entry| {
        !entry.is_empty()
            && entry.bytes().all(|byte| byte.is_ascii_digit())
            && entry.parse::<u64>().is_ok()
    })
}

fn validate_bounded_csv(value: &str, entry_validator: impl Fn(&str) -> bool) -> bool {
    if value.len() > 256 {
        return false;
    }
    let mut count = 0usize;
    for entry in value.split(',') {
        if entry.is_empty() || !entry_validator(entry) {
            return false;
        }
        count += 1;
        if count > 64 {
            return false;
        }
    }
    count > 0
}

const SAFE_TOKEN_INTEGER_OR_NULL_KEYS: &[&str] = &[
    "context_window_tokens",
    "prompt_tokens",
    "requested_output_tokens",
    "required_total_tokens",
    "max_tokens",
    "native_context_tokens",
    "maximum_validated_context_tokens",
    "requested_context_tokens",
    "original_context_tokens",
    "context_size_tokens",
    "qwen_yarn_requested_context_tokens",
    "qwen_yarn_original_context_tokens",
    "plain_completion_prompt_token_count",
    "plain_completion_prompt_tokenization_variant_count",
    "plain_completion_prompt_tokenization_selected_token_count",
    "api_v1_readiness_context_window_tokens",
    "api_v1_readiness_prompt_tokens",
    "api_v1_readiness_yarn_requested_context_tokens",
    "api_v1_readiness_yarn_original_context_tokens",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_token_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count",
];

const SAFE_TOKEN_BOOL_OR_NULL_KEYS: &[&str] = &[
    "plain_completion_accepts_max_tokens_kwarg",
    "plain_completion_prompt_tokenization_special",
    "plain_completion_prompt_tokenization_attempted",
    "plain_completion_prompt_tokenization_selected_special",
    "api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_attempted",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special",
];

const SAFE_TOKEN_IDENTIFIER_OR_NULL_KEYS: &[&str] = &[
    "plain_completion_prompt_tokenization_error_category",
    "plain_completion_prompt_tokenization_method",
    "plain_completion_prompt_tokenization_selected_variant",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_error_category",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant",
];

const SAFE_TOKEN_CSV_IDENTIFIER_OR_NULL_KEYS: &[&str] = &[
    "plain_completion_prompt_tokenization_variant_ids",
    "plain_completion_prompt_tokenization_special_values",
    "plain_completion_attempt_tokenization_variants",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_ids",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special_values",
    "api_v1_readiness_completion_smoke_plain_completion_attempt_tokenization_variants",
];

const SAFE_TOKEN_CSV_INTEGER_OR_NULL_KEYS: &[&str] = &[
    "plain_completion_prompt_tokenization_token_counts",
    "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_token_counts",
];

fn sanitize_url_display(value: &str) -> String {
    let trimmed = value.trim_matches(|ch: char| matches!(ch, '\'' | '"' | ',' | ';' | ')' | '('));
    let without_fragment = trimmed.split('#').next().unwrap_or(trimmed);
    let without_query = without_fragment
        .split('?')
        .next()
        .unwrap_or(without_fragment);
    if let Some((scheme, rest)) = without_query.split_once("://") {
        let authority = rest.split('/').next().unwrap_or(rest);
        let safe_authority = authority.rsplit('@').next().unwrap_or(authority);
        if !scheme.is_empty() && !safe_authority.is_empty() {
            return format!("{scheme}://{safe_authority}");
        }
    }
    "<url>".into()
}

fn sanitize_path_display(value: &str) -> String {
    let trimmed = value.trim_matches(|ch: char| matches!(ch, '\'' | '"' | ',' | ';' | ')' | '('));
    let path = Path::new(trimmed);
    if trimmed.starts_with('/') && trimmed.split('/').filter(|part| !part.is_empty()).count() <= 2 {
        return "<path>".into();
    }
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty());
    match file_name {
        Some(name) => format!("<path:{name}>"),
        None => "<path>".into(),
    }
}

fn is_path_like(value: &str) -> bool {
    let trimmed = value.trim_matches(|ch: char| matches!(ch, '\'' | '"' | ',' | ';' | ')' | '('));
    trimmed.starts_with('/')
        || trimmed.starts_with("~/")
        || trimmed.starts_with("file://")
        || trimmed.contains('/')
        || (trimmed.len() > 2
            && trimmed.as_bytes()[1] == b':'
            && (trimmed.as_bytes()[2] == b'/' || trimmed.as_bytes()[2] == b'\\')
            && trimmed.as_bytes()[0].is_ascii_alphabetic())
}

fn current_time_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as u64)
        .unwrap_or_default()
}

fn sanitize_filename_component(value: &str) -> String {
    let sanitized: String = value
        .chars()
        .filter(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_'))
        .collect();
    if sanitized.is_empty() {
        "unknown".into()
    } else {
        sanitized
    }
}

fn sanitize_log_line(line: &str) -> String {
    line.chars()
        .map(|ch| {
            if ch.is_control() && ch != '\t' {
                ' '
            } else {
                ch
            }
        })
        .collect()
}

#[cfg(target_os = "macos")]
fn quote_applescript_string(value: &str) -> String {
    format!("\"{}\"", value.replace('\\', "\\\\").replace('\"', "\\\""))
}

#[cfg(target_os = "windows")]
fn quote_powershell_single_string(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn compute_operator_log_path_sanitizes_session_and_preserves_spaces_in_dir() {
        let temp = TempDir::new().expect("tempdir");
        let dir = temp.path().join("Token Place Logs");
        let path = compute_operator_log_path(&dir, "session/../../abc 123!");
        assert_eq!(path.parent(), Some(dir.as_path()));
        assert_eq!(
            path.file_name().and_then(|name| name.to_str()),
            Some("compute-node-sessionabc123.log")
        );
    }

    #[test]
    fn posix_tail_script_quotes_paths_with_spaces_and_quotes() {
        let path = Path::new("/Users/Example User/Library/Logs/token.place/compute node's.log");
        let script = tail_terminal_script(path);
        assert!(script
            .contains("'/Users/Example User/Library/Logs/token.place/compute node'\\''s.log'"));
        assert!(script.contains("tail -n 200 -F"));
        assert!(!script.contains("; rm -rf"));
    }

    #[test]
    fn create_uses_unique_non_appended_session_files() {
        let temp = TempDir::new().expect("tempdir");
        let (first_path, mut first_file) =
            create_unique_operator_log_file(temp.path(), "1").expect("first log");
        writeln!(first_file, "stale").expect("write first");
        let (second_path, _second_file) =
            create_unique_operator_log_file(temp.path(), "1").expect("second log");

        assert_ne!(first_path, second_path);
        assert!(first_path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or_default()
            .starts_with("compute-node-1-"));
        assert_eq!(
            fs::read_to_string(second_path).expect("second contents"),
            ""
        );
    }

    #[test]
    fn log_sink_append_mode_preserves_interleaved_lifecycle_appends() {
        let temp = TempDir::new().expect("tempdir");
        let (path, file) = create_unique_operator_log_file(temp.path(), "interleave")
            .expect("create operator log");
        let sink = OperatorLogSink {
            path: path.clone(),
            file: Arc::new(Mutex::new(file)),
        };

        sink.append_line("desktop.compute_node.stdout", "first bridge line");
        append_line_to_path(
            &path,
            "desktop.compute_node.stop_requested",
            "operator_session_id=interleave",
        )
        .expect("append lifecycle line");
        sink.append_line("desktop.compute_node.stdout", "second bridge line");

        let raw = fs::read_to_string(path).expect("log contents");
        assert!(raw.contains("desktop.compute_node.stdout first bridge line"));
        assert!(raw.contains("desktop.compute_node.stop_requested operator_session_id=interleave"));
        assert!(raw.contains("desktop.compute_node.stdout second bridge line"));
        let lifecycle_index = raw
            .find("desktop.compute_node.stop_requested")
            .expect("lifecycle line index");
        let second_sink_index = raw
            .find("desktop.compute_node.stdout second bridge line")
            .expect("second sink line index");
        assert!(
            lifecycle_index < second_sink_index,
            "lifecycle append must not be overwritten by subsequent sink writes: {raw}"
        );
    }

    #[test]
    fn sanitize_operator_diagnostic_line_sanitizes_whole_json_before_tokenization() {
        let sanitized = sanitize_operator_diagnostic_line(
            r#"{"type":"x", "api_key":"secret value", "nested":{"token":"secret token"}, "safe":"hello world"}"#,
        );
        let payload: serde_json::Value =
            serde_json::from_str(&sanitized).expect("sanitized json remains parseable");

        assert_eq!(
            payload.get("api_key").and_then(serde_json::Value::as_str),
            Some("<redacted>")
        );
        assert_eq!(
            payload
                .get("nested")
                .and_then(|nested| nested.get("token"))
                .and_then(serde_json::Value::as_str),
            Some("<redacted>")
        );
        assert_eq!(
            payload.get("safe").and_then(serde_json::Value::as_str),
            Some("hello world")
        );
        assert!(!sanitized.contains("secret value"));
        assert!(!sanitized.contains("secret token"));
    }

    #[test]
    fn sanitize_operator_diagnostic_line_replaces_oversized_json_without_parsing() {
        let oversized = format!(r#"{{"safe":"{}"}}"#, "x".repeat(5000));
        let sanitized = sanitize_operator_diagnostic_line(&oversized);
        let payload: serde_json::Value =
            serde_json::from_str(&sanitized).expect("truncation marker json");

        assert_eq!(
            payload
                .get("safe_truncation")
                .and_then(serde_json::Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn sanitize_operator_diagnostic_line_preserves_safe_token_metadata() {
        let sanitized = sanitize_operator_diagnostic_line(
            &serde_json::json!({
                "context_window_tokens": 65536,
                "api_v1_readiness_yarn_requested_context_tokens": 65536,
                "api_v1_readiness_yarn_original_context_tokens": 32768,
                "api_v1_readiness_completion_smoke_plain_completion_prompt_token_count": 50,
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count": 28,
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count": 2,
                "api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg": true,
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special": false,
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method": "llama.tokenize",
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant": "tokenize_add_bos_false_special_false",
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_ids": "tokenize_add_bos_false_special_false,tokenize_add_bos_false_special_true",
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_token_counts": "50,28",
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special_values": "false,true",
            })
            .to_string(),
        );
        let payload: serde_json::Value = serde_json::from_str(&sanitized).expect("json");

        assert_eq!(payload["context_window_tokens"].as_u64(), Some(65536));
        assert_eq!(
            payload["api_v1_readiness_yarn_requested_context_tokens"].as_u64(),
            Some(65536)
        );
        assert_eq!(
            payload["api_v1_readiness_yarn_original_context_tokens"].as_u64(),
            Some(32768)
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_prompt_token_count"]
                .as_u64(),
            Some(50)
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count"].as_u64(),
            Some(28)
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_count"].as_u64(),
            Some(2)
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_accepts_max_tokens_kwarg"]
                .as_bool(),
            Some(true)
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_special"].as_bool(),
            Some(false)
        );
        assert_eq!(
            payload
                ["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method"]
                .as_str(),
            Some("llama.tokenize")
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_variant"].as_str(),
            Some("tokenize_add_bos_false_special_false")
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_ids"].as_str(),
            Some("tokenize_add_bos_false_special_false,tokenize_add_bos_false_special_true")
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_token_counts"].as_str(),
            Some("50,28")
        );
        assert_eq!(
            payload["api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_special_values"].as_str(),
            Some("false,true")
        );
        assert!(!sanitized.contains("<redacted>"));
    }

    #[test]
    fn sanitize_operator_diagnostic_line_redacts_real_token_material() {
        let sanitized = sanitize_operator_diagnostic_line(
            &serde_json::json!({
                "token": "SECRET_A",
                "tokens": ["SECRET_B"],
                "token_ids": [1, 2, 3],
                "prompt_token_ids": ["SECRET_C"],
                "access_token": "SECRET_D",
                "refresh_token": {"nested": "SECRET_E"},
                "cancel_token": 42,
                "session_token": "SECRET_F",
                "api_token": "SECRET_G"
            })
            .to_string(),
        );
        let payload: serde_json::Value = serde_json::from_str(&sanitized).expect("json");

        for key in [
            "token",
            "tokens",
            "token_ids",
            "prompt_token_ids",
            "access_token",
            "refresh_token",
            "cancel_token",
            "session_token",
            "api_token",
        ] {
            assert_eq!(payload[key].as_str(), Some("<redacted>"), "{key}");
        }
        assert!(!sanitized.contains("SECRET_"));
    }

    #[test]
    fn sanitize_operator_diagnostic_line_redacts_unsafe_safe_token_metadata_values() {
        let many_entries = (0..65)
            .map(|idx| format!("id{idx}"))
            .collect::<Vec<_>>()
            .join(",");
        let sanitized = sanitize_operator_diagnostic_line(
            &serde_json::json!({
                "context_window_tokens": "SECRET",
                "api_v1_readiness_yarn_requested_context_tokens": -1,
                "api_v1_readiness_yarn_original_context_tokens": 32768.5,
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_selected_token_count": [28],
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_token_counts": "50,text",
                "plain_completion_prompt_tokenization_method": "has spaces",
                "plain_completion_prompt_tokenization_error_category": "has\"quote",
                "plain_completion_prompt_tokenization_selected_variant": "has\\backslash",
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_method": "bad\u{0001}control",
                "api_v1_readiness_completion_smoke_plain_completion_prompt_tokenization_variant_ids": many_entries,
            })
            .to_string(),
        );
        let payload: serde_json::Value = serde_json::from_str(&sanitized).expect("json");
        let object = payload.as_object().expect("object");
        for (key, value) in object {
            assert_eq!(value.as_str(), Some("<redacted>"), "{key}");
        }
        assert!(!sanitized.contains("SECRET"));
    }

    #[test]
    fn sanitize_operator_diagnostic_line_preserves_nested_safe_metadata_and_redacts_nested_tokens()
    {
        let sanitized = sanitize_operator_diagnostic_line(
            &serde_json::json!({
                "outer": {
                    "context_window_tokens": 65536,
                    "token": "SECRET_A",
                    "items": [
                        {"api_v1_readiness_yarn_original_context_tokens": 32768},
                        {"prompt_token_ids": ["SECRET_B"]}
                    ]
                }
            })
            .to_string(),
        );
        let payload: serde_json::Value = serde_json::from_str(&sanitized).expect("json");

        assert_eq!(
            payload["outer"]["context_window_tokens"].as_u64(),
            Some(65536)
        );
        assert_eq!(payload["outer"]["token"].as_str(), Some("<redacted>"));
        assert_eq!(
            payload["outer"]["items"][0]["api_v1_readiness_yarn_original_context_tokens"].as_u64(),
            Some(32768)
        );
        assert_eq!(
            payload["outer"]["items"][1]["prompt_token_ids"].as_str(),
            Some("<redacted>")
        );
        assert!(!sanitized.contains("SECRET_"));
    }

    #[test]
    fn read_log_tail_reads_only_requested_suffix() {
        let temp = TempDir::new().expect("tempdir");
        let path = temp.path().join("operator.log");
        fs::write(&path, "0123456789abcdef").expect("write log");

        assert_eq!(read_log_tail(&path, 6).expect("tail"), "abcdef");
        assert_eq!(read_log_tail(&path, 64).expect("tail"), "0123456789abcdef");
    }

    #[test]
    fn log_sink_writes_lines() {
        let temp = TempDir::new().expect("tempdir");
        let path = compute_operator_log_path(temp.path(), "42");
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .expect("file");
        let sink = OperatorLogSink {
            path: path.clone(),
            file: Arc::new(Mutex::new(file)),
        };
        sink.append_line("desktop.compute_node.stderr", "bridge stderr line");
        let raw = fs::read_to_string(path).expect("log");
        assert!(raw.contains("desktop.compute_node.stderr bridge stderr line"));
    }
}
