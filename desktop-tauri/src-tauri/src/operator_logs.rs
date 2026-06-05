use std::fs::{self, File, OpenOptions};
use std::io::Write;
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
        let path = compute_operator_log_path(&dir, session_id);
        let file = OpenOptions::new().create(true).append(true).open(&path)?;
        Ok(Self {
            path,
            file: Arc::new(Mutex::new(file)),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append_line(&self, source: &str, line: &str) {
        let sanitized = sanitize_log_line(line);
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
        sanitize_log_line(line)
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
    let bytes = fs::read(log_path)?;
    let start = bytes.len().saturating_sub(max_bytes);
    Ok(String::from_utf8_lossy(&bytes[start..]).into_owned())
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
        let path = Path::new("/Users/Daniel Smith/Library/Logs/token.place/compute node's.log");
        let script = tail_terminal_script(path);
        assert!(script
            .contains("'/Users/Daniel Smith/Library/Logs/token.place/compute node'\\''s.log'"));
        assert!(script.contains("tail -n 200 -F"));
        assert!(!script.contains("; rm -rf"));
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
