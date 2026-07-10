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
        if serde_json::from_str::<serde_json::Value>(token).is_ok() {
            return r#"{"type":"operator_diagnostic_json_omitted","truncated":true}"#.to_string();
        }
        return token.chars().take(4096).collect();
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
