use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use tauri::Manager;
use tokio::io::AsyncWriteExt;
use tokio::sync::Mutex;

#[derive(Clone)]
pub struct SessionLogFile {
    path: PathBuf,
    file: Arc<Mutex<tokio::fs::File>>,
}

impl SessionLogFile {
    pub async fn create_in_dir(log_dir: &Path, session_id: &str) -> anyhow::Result<Self> {
        tokio::fs::create_dir_all(log_dir).await?;
        let file_name = format!("compute-node-{}.log", sanitize_log_file_token(session_id));
        let path = log_dir.join(file_name);
        Self::append_existing_or_create(path).await
    }

    pub async fn append_existing_or_create(path: PathBuf) -> anyhow::Result<Self> {
        let file = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .await?;
        Ok(Self {
            path,
            file: Arc::new(Mutex::new(file)),
        })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn path_string(&self) -> String {
        self.path.to_string_lossy().into_owned()
    }

    pub async fn write_line(&self, line: impl AsRef<str>) {
        let mut file = self.file.lock().await;
        let _ = file.write_all(line.as_ref().as_bytes()).await;
        let _ = file.write_all(b"\n").await;
        let _ = file.flush().await;
    }
}

pub fn app_operator_log_dir(app: &tauri::AppHandle) -> anyhow::Result<PathBuf> {
    let base = app
        .path()
        .app_log_dir()
        .or_else(|_| app.path().app_data_dir().map(|dir| dir.join("logs")))
        .map_err(|e| anyhow::anyhow!("log path error: {e}"))?;
    Ok(base.join("operators"))
}

pub fn sanitize_log_file_token(value: &str) -> String {
    let sanitized = value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.') {
                ch
            } else {
                '-'
            }
        })
        .collect::<String>();
    let trimmed = sanitized.trim_matches('-');
    if trimmed.is_empty() {
        "unknown".into()
    } else {
        trimmed.chars().take(80).collect()
    }
}

pub fn shell_single_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\\''"))
}

pub fn macos_terminal_tail_script(log_path: &Path) -> String {
    let quoted_path = shell_single_quote(&log_path.to_string_lossy());
    let tail_command = format!(
        "printf 'Tailing token.place operator log: %s\\n' {quoted_path}; tail -n 200 -F {quoted_path}"
    );
    format!(
        "tell application \"Terminal\" to do script {}",
        shell_single_quote(&tail_command)
    )
}

pub fn open_terminal_tailing_log(log_path: &Path) -> anyhow::Result<()> {
    if !log_path.is_file() {
        anyhow::bail!("operator log file does not exist: {}", log_path.display());
    }

    #[cfg(target_os = "macos")]
    {
        let status = Command::new("osascript")
            .arg("-e")
            .arg(macos_terminal_tail_script(log_path))
            .status()?;
        if !status.success() {
            anyhow::bail!("osascript failed with status {status}");
        }
        Ok(())
    }

    #[cfg(not(target_os = "macos"))]
    {
        reveal_log_file(log_path)
    }
}

pub fn reveal_log_file(log_path: &Path) -> anyhow::Result<()> {
    if !log_path.is_file() {
        anyhow::bail!("operator log file does not exist: {}", log_path.display());
    }

    #[cfg(target_os = "macos")]
    let mut command = {
        let mut command = Command::new("open");
        command.arg("-R").arg(log_path);
        command
    };

    #[cfg(target_os = "windows")]
    let mut command = {
        let mut command = Command::new("explorer.exe");
        command.arg(format!("/select,{}", log_path.display()));
        command
    };

    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let mut command = {
        let parent = log_path.parent().unwrap_or_else(|| Path::new("."));
        let mut command = Command::new("xdg-open");
        command.arg(parent);
        command
    };

    let status = command.status()?;
    if !status.success() {
        anyhow::bail!("open log command failed with status {status}");
    }
    Ok(())
}

#[allow(dead_code)]
pub async fn read_log_tail(log_path: &Path, max_bytes: u64) -> anyhow::Result<String> {
    if !log_path.is_file() {
        anyhow::bail!("operator log file does not exist: {}", log_path.display());
    }
    let metadata = tokio::fs::metadata(log_path).await?;
    let start = metadata.len().saturating_sub(max_bytes);
    let mut file = tokio::fs::File::open(log_path).await?;
    use tokio::io::{AsyncReadExt, AsyncSeekExt};
    file.seek(std::io::SeekFrom::Start(start)).await?;
    let mut buf = Vec::new();
    file.read_to_end(&mut buf).await?;
    Ok(String::from_utf8_lossy(&buf).into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[tokio::test]
    async fn session_log_file_creates_path_under_log_dir() {
        let temp = TempDir::new().expect("tempdir");
        let log_dir = temp.path().join("Token Place Logs").join("operators");
        let log = SessionLogFile::create_in_dir(&log_dir, "session 1/../secret")
            .await
            .expect("create log");
        log.write_line("desktop.compute_node.stderr line=bridge-failure")
            .await;

        assert!(log.path().is_file());
        assert!(log.path().starts_with(&log_dir));
        assert!(log.path_string().contains("session-1"));
        assert!(log.path_string().contains("secret"));
        assert!(!log
            .path()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .contains('/'));
        let contents = tokio::fs::read_to_string(log.path())
            .await
            .expect("read log");
        assert!(contents.contains("bridge-failure"));
    }

    #[test]
    fn macos_tail_script_shell_quotes_paths_with_spaces_and_quotes() {
        let path = Path::new("/Users/example/Library/Logs/Token Place/operators/compute 'one'.log");
        let script = macos_terminal_tail_script(path);

        assert!(script.contains("Terminal"));
        assert!(script.contains("Token Place"));
        assert!(script.contains("'\\''one'\\''"));
        assert!(!script.contains("; rm -rf"));
    }
}
