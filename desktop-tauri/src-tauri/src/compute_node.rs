use crate::backend::ComputeMode;
use crate::python_runtime::{resolve_python_launcher, resolve_runtime_import_root, PythonLauncher};
use crate::subprocess_logging::{SubprocessLogFilter, SubprocessLogPolicy};
use serde::{Deserialize, Serialize};
use serde_json::Value;
#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComputeNodeRequest {
    pub model_path: String,
    pub relay_base_url: String,
    pub mode: ComputeMode,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ComputeNodeStatus {
    pub running: bool,
    pub registered: bool,
    pub active_relay_url: String,
    pub requested_mode: String,
    pub effective_mode: String,
    pub backend_available: String,
    pub backend_selected: String,
    pub backend_used: String,
    pub fallback_reason: Option<String>,
    pub model_path: String,
    pub last_error: Option<String>,
}

#[derive(Clone, Default)]
pub struct ComputeNodeState {
    pub child: Arc<Mutex<Option<Child>>>,
    pub stdin: Arc<Mutex<Option<ChildStdin>>>,
    pub status: Arc<Mutex<ComputeNodeStatus>>,
    pub lifecycle_lock: Arc<Mutex<()>>,
}

fn parse_compute_node_event_line(line: &str) -> Result<Value, serde_json::Error> {
    serde_json::from_str::<Value>(line)
}

fn build_bridge_command(
    bridge_path: &str,
    launcher: Option<PythonLauncher>,
) -> anyhow::Result<Command> {
    if is_python_script(bridge_path) {
        let launcher = launcher.ok_or_else(|| {
            anyhow::anyhow!("missing resolved Python launcher for compute-node bridge script")
        })?;
        return Ok(launcher.command_for_script(bridge_path));
    }

    Ok(Command::new(bridge_path))
}

fn is_python_script(path: &str) -> bool {
    Path::new(path)
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("py"))
}

fn bridge_script_candidates(
    exe_path: Option<&Path>,
    manifest_dir: &Path,
) -> Vec<std::path::PathBuf> {
    let mut candidates = Vec::new();

    if let Some(exe_path) = exe_path {
        if let Some(exe_dir) = exe_path.parent() {
            candidates.push(
                exe_dir
                    .join("resources")
                    .join("python")
                    .join("compute_node_bridge.py"),
            );
            candidates.push(exe_dir.join("python").join("compute_node_bridge.py"));
            if let Some(parent_dir) = exe_dir.parent() {
                candidates.push(
                    parent_dir
                        .join("Resources")
                        .join("python")
                        .join("compute_node_bridge.py"),
                );
                candidates.push(
                    parent_dir
                        .join("resources")
                        .join("python")
                        .join("compute_node_bridge.py"),
                );
            }
        }
    }

    candidates.push(manifest_dir.join("python").join("compute_node_bridge.py"));
    candidates
}

fn resolve_bridge_script() -> String {
    let exe_path = std::env::current_exe().ok();
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let candidates = bridge_script_candidates(exe_path.as_deref(), manifest_dir);

    if let Some(path) = first_existing_script(candidates) {
        return path;
    }

    "python/compute_node_bridge.py".into()
}

fn first_existing_script(candidates: Vec<std::path::PathBuf>) -> Option<String> {
    candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .map(|candidate| candidate.to_string_lossy().into_owned())
}

fn configure_runtime_pythonpath(command: &mut Command, manifest_dir: &Path, bridge_script: &str) {
    if let Some(import_root) =
        resolve_runtime_import_root(Some(Path::new(bridge_script)), manifest_dir)
    {
        command.env("TOKEN_PLACE_PYTHON_IMPORT_ROOT", &import_root);
        match std::env::var("PYTHONPATH") {
            Ok(existing) if !existing.trim().is_empty() => {
                let mut components = vec![import_root.clone()];
                components.extend(std::env::split_paths(&existing));
                if let Ok(joined) = std::env::join_paths(components) {
                    command.env("PYTHONPATH", joined);
                } else {
                    command.env("PYTHONPATH", import_root);
                }
            }
            _ => {
                command.env("PYTHONPATH", import_root);
            }
        }
    }
}

fn startup_failure_status(request: &ComputeNodeRequest, last_error: String) -> ComputeNodeStatus {
    ComputeNodeStatus {
        running: false,
        registered: false,
        active_relay_url: request.relay_base_url.clone(),
        requested_mode: format!("{:?}", request.mode).to_lowercase(),
        effective_mode: "cpu".into(),
        backend_available: "unknown".into(),
        backend_selected: "cpu".into(),
        backend_used: "cpu".into(),
        fallback_reason: None,
        model_path: request.model_path.clone(),
        last_error: Some(last_error),
    }
}

fn update_status_from_event(status: &mut ComputeNodeStatus, payload: &Value) {
    if let Some(running) = payload.get("running").and_then(Value::as_bool) {
        status.running = running;
    }
    if let Some(registered) = payload.get("registered").and_then(Value::as_bool) {
        status.registered = registered;
    }
    if let Some(active_relay_url) = payload.get("active_relay_url").and_then(Value::as_str) {
        status.active_relay_url = active_relay_url.into();
    }
    if let Some(requested_mode) = payload.get("requested_mode").and_then(Value::as_str) {
        status.requested_mode = requested_mode.into();
    }
    if let Some(effective_mode) = payload.get("effective_mode").and_then(Value::as_str) {
        status.effective_mode = effective_mode.into();
    }
    if let Some(backend_available) = payload.get("backend_available").and_then(Value::as_str) {
        status.backend_available = backend_available.into();
    }
    if let Some(backend_selected) = payload.get("backend_selected").and_then(Value::as_str) {
        status.backend_selected = backend_selected.into();
    }
    if let Some(backend_used) = payload.get("backend_used").and_then(Value::as_str) {
        status.backend_used = backend_used.into();
    }
    if payload.get("fallback_reason").is_some() {
        status.fallback_reason = payload
            .get("fallback_reason")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if let Some(model_path) = payload.get("model_path").and_then(Value::as_str) {
        status.model_path = model_path.into();
    }
    if payload.get("last_error").is_some() {
        status.last_error = payload
            .get("last_error")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned);
    }
    if payload.get("type").and_then(Value::as_str) == Some("error") {
        status.last_error = payload
            .get("message")
            .and_then(Value::as_str)
            .map(ToOwned::to_owned)
            .or_else(|| Some("compute-node bridge error".into()));
    }
}

fn bridge_exit_status_label(exit_status: std::process::ExitStatus) -> String {
    if let Some(code) = exit_status.code() {
        return code.to_string();
    }
    #[cfg(unix)]
    if let Some(signal) = exit_status.signal() {
        return format!("signal {signal}");
    }
    format!("{exit_status:?}")
}

fn bridge_exit_error(
    exit_status: std::process::ExitStatus,
    saw_startup_event: bool,
) -> Option<String> {
    let status_label = bridge_exit_status_label(exit_status);
    if !saw_startup_event {
        return Some(format!(
            "compute-node bridge exited with status {status_label} before emitting a startup \
             event; see desktop.compute_node.stderr logs"
        ));
    }
    if exit_status.success() {
        return None;
    }
    Some(format!(
        "compute-node bridge exited with status {status_label}; \
         see desktop.compute_node.stderr logs"
    ))
}

async fn drain_compute_node_stderr<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
    policy: SubprocessLogPolicy,
) -> anyhow::Result<()> {
    let mut lines = BufReader::new(reader).lines();
    let mut filter = SubprocessLogFilter::new("compute_node", policy);
    while let Some(line) = lines.next_line().await? {
        if filter.should_emit(&line) {
            eprintln!("desktop.compute_node.stderr line={line}");
        }
    }
    Ok(())
}

pub async fn start_compute_node(
    app: AppHandle,
    state: ComputeNodeState,
    request: ComputeNodeRequest,
) -> anyhow::Result<()> {
    let _lifecycle_lock = state.lifecycle_lock.lock().await;
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));

    {
        let mut child_slot = state.child.lock().await;
        if child_slot
            .as_mut()
            .is_some_and(|child| child.try_wait().ok().flatten().is_none())
        {
            anyhow::bail!("compute node already running; stop it before starting a new session");
        }
        *child_slot = None;
        *state.stdin.lock().await = None;
    }

    let bridge_script = resolve_bridge_script();
    let launcher = if is_python_script(&bridge_script) {
        match tokio::task::spawn_blocking(|| resolve_python_launcher("TOKEN_PLACE_SIDECAR_PYTHON"))
            .await
        {
            Ok(result) => match result {
                Ok(launcher) => Some(launcher),
                Err(err) => {
                    {
                        let mut status = state.status.lock().await;
                        *status = startup_failure_status(&request, err.to_string());
                    }
                    return Err(err);
                }
            },
            Err(err) => {
                let err = anyhow::anyhow!("python launcher resolver task failed: {err}");
                {
                    let mut status = state.status.lock().await;
                    *status = startup_failure_status(&request, err.to_string());
                }
                return Err(err);
            }
        }
    } else {
        None
    };

    let mut bridge_command = match build_bridge_command(&bridge_script, launcher) {
        Ok(command) => command,
        Err(err) => {
            {
                let mut status = state.status.lock().await;
                *status = startup_failure_status(&request, err.to_string());
            }
            return Err(err);
        }
    };
    configure_runtime_pythonpath(&mut bridge_command, manifest_dir, &bridge_script);

    let spawn_result = bridge_command
        .arg("--model")
        .arg(&request.model_path)
        .arg("--mode")
        .arg(format!("{:?}", request.mode).to_lowercase())
        .arg("--relay-url")
        .arg(&request.relay_base_url)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();

    let mut child = match spawn_result {
        Ok(child) => child,
        Err(err) => {
            {
                let mut status = state.status.lock().await;
                *status = startup_failure_status(
                    &request,
                    format!("failed to start compute-node bridge: {err}"),
                );
            }
            *state.child.lock().await = None;
            *state.stdin.lock().await = None;
            anyhow::bail!("failed to spawn compute-node bridge: {err}");
        }
    };

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute-node bridge stdout"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute-node bridge stderr"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing compute-node bridge stdin"))?;

    {
        let mut child_slot = state.child.lock().await;
        *child_slot = Some(child);
        let mut stdin_slot = state.stdin.lock().await;
        *stdin_slot = Some(stdin);
        let mut status = state.status.lock().await;
        *status = ComputeNodeStatus {
            running: true,
            registered: false,
            active_relay_url: request.relay_base_url.clone(),
            requested_mode: format!("{:?}", request.mode).to_lowercase(),
            effective_mode: "cpu".into(),
            backend_available: "unknown".into(),
            backend_selected: "cpu".into(),
            backend_used: "cpu".into(),
            fallback_reason: None,
            model_path: request.model_path.clone(),
            last_error: None,
        };
    }

    let log_policy = SubprocessLogPolicy::from_env();
    let stderr_task = tokio::spawn(async move {
        if let Err(err) = drain_compute_node_stderr(stderr, log_policy).await {
            eprintln!("desktop.compute_node.stderr_error error={err}");
        }
    });

    let mut lines = BufReader::new(stdout).lines();
    let mut saw_error_event = false;
    let mut saw_startup_event = false;
    while let Some(line) = lines.next_line().await? {
        match parse_compute_node_event_line(&line) {
            Ok(payload) => {
                if let Some(event_type) = payload.get("type").and_then(Value::as_str) {
                    if event_type == "error" {
                        saw_error_event = true;
                    }
                    if event_type == "started" || event_type == "status" {
                        saw_startup_event = true;
                    }
                }
                {
                    let mut status = state.status.lock().await;
                    update_status_from_event(&mut status, &payload);
                }
                app.emit("compute_node_event", payload)?;
            }
            Err(err) => {
                eprintln!(
                    "desktop.compute_node.stdout_parse_error error={} line={}",
                    err, line
                );
            }
        }
    }

    if let Err(err) = stderr_task.await {
        eprintln!("desktop.compute_node.stderr_task_join_error error={err}");
    }

    let running_child = {
        let mut child_slot = state.child.lock().await;
        child_slot.take()
    };

    if let Some(mut running_child) = running_child {
        let exit_status = running_child.wait().await?;
        let exit_error = bridge_exit_error(exit_status, saw_startup_event);

        {
            let mut status = state.status.lock().await;
            status.running = false;
            status.registered = false;
            if status.last_error.is_none() {
                status.last_error = exit_error.clone();
            }
        }

        if let Some(last_error) = exit_error {
            if !saw_error_event {
                app.emit(
                    "compute_node_event",
                    serde_json::json!({
                        "type": "error",
                        "running": false,
                        "registered": false,
                        "last_error": last_error,
                    }),
                )?;
            }
        }
    } else {
        let mut status = state.status.lock().await;
        status.running = false;
        status.registered = false;
    }
    *state.stdin.lock().await = None;

    Ok(())
}

pub async fn stop_compute_node(state: ComputeNodeState) -> anyhow::Result<()> {
    let _lifecycle_lock = state.lifecycle_lock.lock().await;

    if let Some(stdin) = state.stdin.lock().await.as_mut() {
        stdin.write_all(b"{\"type\":\"cancel\"}\n").await?;
        stdin.flush().await?;
    }

    let mut should_kill = false;
    for _ in 0..20 {
        let mut child_lock = state.child.lock().await;
        let Some(child) = child_lock.as_mut() else {
            break;
        };

        if child.try_wait()?.is_some() {
            *child_lock = None;
            *state.stdin.lock().await = None;
            let mut status = state.status.lock().await;
            status.running = false;
            status.registered = false;
            return Ok(());
        }

        should_kill = true;
        drop(child_lock);
        tokio::time::sleep(Duration::from_millis(50)).await;
    }

    if should_kill {
        let mut child_lock = state.child.lock().await;
        if let Some(child) = child_lock.as_mut() {
            let _ = child.kill().await;
        }
    }

    *state.child.lock().await = None;
    *state.stdin.lock().await = None;
    {
        let mut status = state.status.lock().await;
        status.running = false;
        status.registered = false;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command as StdCommand;
    use std::process::ExitStatus;
    use tempfile::TempDir;
    use tokio::io::AsyncBufReadExt;
    use tokio::process::Command;

    fn success_exit_status() -> ExitStatus {
        #[cfg(windows)]
        {
            StdCommand::new("cmd")
                .args(["/C", "exit", "0"])
                .status()
                .expect("status")
        }
        #[cfg(not(windows))]
        {
            StdCommand::new("sh")
                .args(["-c", "exit 0"])
                .status()
                .expect("status")
        }
    }

    #[tokio::test]
    async fn malformed_stdout_lines_are_ignored_without_blocking_valid_events() {
        let reader = BufReader::new(
            b"{\"type\":\"status\",\"running\":true}\nnot-json\n{\"type\":\"error\",\"message\":\"boom\"}\n"
                .as_slice(),
        );
        let mut lines = reader.lines();
        let mut event_types = Vec::new();

        while let Some(line) = lines.next_line().await.expect("read line") {
            if let Ok(payload) = parse_compute_node_event_line(&line) {
                let event_type = payload
                    .get("type")
                    .and_then(Value::as_str)
                    .expect("event type");
                event_types.push(event_type.to_string());
            }
        }

        assert_eq!(event_types, vec!["status".to_string(), "error".to_string()]);
    }

    #[tokio::test]
    async fn drain_compute_node_stderr_reads_all_lines() {
        let mut child = {
            #[cfg(windows)]
            {
                let mut command = Command::new("cmd");
                command.args(["/C", "echo bridge-failure 1>&2"]);
                command
            }
            #[cfg(not(windows))]
            {
                let mut command = Command::new("sh");
                command.args(["-c", "echo bridge-failure 1>&2"]);
                command
            }
        }
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn stderr script");

        let stderr = child.stderr.take().expect("stderr");
        drain_compute_node_stderr(stderr, SubprocessLogPolicy { verbose_raw: true })
            .await
            .expect("drain stderr");
        let status = child.wait().await.expect("wait child");
        assert!(status.success());
    }

    #[test]
    fn startup_failure_status_records_resolver_error_and_not_running() {
        let request = ComputeNodeRequest {
            model_path: "model.gguf".into(),
            relay_base_url: "https://relay.example".into(),
            mode: ComputeMode::Cpu,
        };
        let status = startup_failure_status(
            &request,
            "no usable Python 3 interpreter found for desktop Python subprocess".into(),
        );

        assert!(!status.running);
        assert!(!status.registered);
        assert_eq!(
            status.last_error.as_deref(),
            Some("no usable Python 3 interpreter found for desktop Python subprocess")
        );
        assert_eq!(status.active_relay_url, request.relay_base_url);
        assert_eq!(status.model_path, request.model_path);
    }

    #[test]
    fn bridge_exit_error_reports_missing_startup_event_even_on_clean_exit() {
        let exit_status = success_exit_status();
        assert!(exit_status.success());

        let last_error = bridge_exit_error(exit_status, false);
        assert!(last_error.is_some());
        assert!(last_error
            .as_deref()
            .is_some_and(|message| message.contains("before emitting a startup event")));
    }

    #[test]
    fn bridge_exit_error_is_none_after_started_event_and_clean_exit() {
        let exit_status = success_exit_status();
        assert!(exit_status.success());

        assert!(bridge_exit_error(exit_status, true).is_none());
    }

    #[test]
    fn bridge_script_candidates_include_packaged_resource_locations() {
        let temp = TempDir::new().expect("tempdir");
        let app_root = temp.path().join("Token Place.app");
        let exe_dir = app_root.join("Contents").join("MacOS");
        let exe_path = exe_dir.join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");
        let candidates = bridge_script_candidates(Some(&exe_path), &manifest_dir);

        assert!(candidates
            .iter()
            .any(|candidate| candidate.ends_with("resources/python/compute_node_bridge.py")));
        assert!(candidates
            .iter()
            .any(|candidate| candidate.ends_with("Resources/python/compute_node_bridge.py")));
        assert_eq!(
            candidates.last().expect("manifest candidate"),
            &manifest_dir.join("python").join("compute_node_bridge.py")
        );
    }

    #[test]
    fn first_existing_script_finds_packaged_resource_bridge_path() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");
        let bridge = resources_dir.join("compute_node_bridge.py");
        std::fs::write(&bridge, "print('ok')\n").expect("write bridge");

        let exe_path = exe_dir.join("token.place.exe");
        let candidates = bridge_script_candidates(Some(&exe_path), temp.path());
        let resolved = first_existing_script(candidates).expect("resolved bridge path");

        assert_eq!(Path::new(&resolved), bridge);
    }

    #[test]
    fn first_existing_script_prefers_resources_over_exe_python_bridge_path() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let exe_python_dir = exe_dir.join("python");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&exe_python_dir).expect("create exe python dir");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");

        let exe_bridge = exe_python_dir.join("compute_node_bridge.py");
        std::fs::write(&exe_bridge, "print('exe')\n").expect("write exe bridge");
        let resources_bridge = resources_dir.join("compute_node_bridge.py");
        std::fs::write(&resources_bridge, "print('resources')\n").expect("write resources bridge");

        let exe_path = exe_dir.join("token.place");
        let candidates = bridge_script_candidates(Some(&exe_path), temp.path());
        let resolved = first_existing_script(candidates).expect("resolved bridge path");

        assert_eq!(Path::new(&resolved), resources_bridge);
    }
}
