use crate::backend::ComputeMode;
use crate::python_runtime::{resolve_python_launcher, resolve_runtime_import_root, PythonLauncher};
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::Mutex;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceRequest {
    pub request_id: String,
    pub model_path: String,
    pub prompt: String,
    pub mode: ComputeMode,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type")]
pub enum SidecarEvent {
    #[serde(rename = "started")]
    Started,
    #[serde(rename = "token")]
    Token { text: String },
    #[serde(rename = "done")]
    Done,
    #[serde(rename = "canceled")]
    Canceled,
    #[serde(rename = "error")]
    Error { code: String, message: String },
}

#[derive(Debug, Clone, Serialize)]
pub struct UiInferenceEvent {
    pub request_id: String,
    #[serde(flatten)]
    pub event: SidecarEvent,
}

#[derive(Clone, Default)]
pub struct SidecarState {
    pub child: Arc<Mutex<Option<Child>>>,
    pub stdin: Arc<Mutex<Option<ChildStdin>>>,
}

#[derive(Default, Debug)]
struct StderrSummary {
    backend: Option<String>,
    device: Option<String>,
    context_size: Option<String>,
    offloaded_layers: Option<String>,
    load_time: Option<String>,
    prompt_throughput: Option<String>,
    eval_throughput: Option<String>,
    fallback_reason: Option<String>,
    noisy_counts: [usize; 4],
}

fn raw_sidecar_logs_enabled() -> bool {
    matches!(
        std::env::var("TOKEN_PLACE_DESKTOP_VERBOSE_RAW_LOGS").as_deref(),
        Ok("1")
    )
}

fn looks_like_warning_or_error(line: &str) -> bool {
    let lower = line.to_ascii_lowercase();
    ["error", "warn", "failed", "exception", "traceback", "fatal"]
        .iter()
        .any(|needle| lower.contains(needle))
}

fn noisy_pattern_bucket(line: &str) -> Option<usize> {
    if line.contains("Dumping metadata keys/values") {
        return Some(0);
    }
    if line.contains("is not marked as EOG") {
        return Some(1);
    }
    if line.contains("load_tensors: layer") || line.contains("load_tensors: offloading layer") {
        return Some(2);
    }
    if line.contains("repack:") {
        return Some(3);
    }
    None
}

fn parse_summary_line(summary: &mut StderrSummary, line: &str) {
    let compact = line.trim().replace('"', "'");
    if summary.context_size.is_none() && line.contains("n_ctx") {
        summary.context_size = Some(compact.clone());
    }
    if summary.offloaded_layers.is_none()
        && line.to_ascii_lowercase().contains("offloaded")
        && line.to_ascii_lowercase().contains("layers")
    {
        summary.offloaded_layers = Some(compact.clone());
    }
    if summary.load_time.is_none()
        && (line.to_ascii_lowercase().contains("load time")
            || line.to_ascii_lowercase().contains("loaded in")
            || line.to_ascii_lowercase().contains("done in"))
    {
        summary.load_time = Some(compact.clone());
    }
    if summary.prompt_throughput.is_none()
        && line.to_ascii_lowercase().contains("tokens per second")
        && line.to_ascii_lowercase().contains("prompt")
    {
        summary.prompt_throughput = Some(compact.clone());
    }
    if summary.eval_throughput.is_none()
        && line.to_ascii_lowercase().contains("tokens per second")
        && line.to_ascii_lowercase().contains("eval")
    {
        summary.eval_throughput = Some(compact.clone());
    }
    if summary.backend.is_none()
        && (line.contains("CUDA")
            || line.contains("Metal")
            || line.contains("CPU")
            || line.to_ascii_lowercase().contains("backend"))
    {
        summary.backend = Some(compact.clone());
    }
    if summary.device.is_none()
        && (line.to_ascii_lowercase().contains("using device")
            || line.to_ascii_lowercase().contains("device"))
    {
        summary.device = Some(compact.clone());
    }
    if summary.fallback_reason.is_none() && line.to_ascii_lowercase().contains("falling back") {
        summary.fallback_reason = Some(compact);
    }
}

pub fn parse_event_line(line: &str) -> Result<SidecarEvent, serde_json::Error> {
    serde_json::from_str::<SidecarEvent>(line)
}

#[cfg(test)]
pub async fn collect_events_from_stdout<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
) -> anyhow::Result<Vec<SidecarEvent>> {
    let mut events = Vec::new();
    let mut lines = BufReader::new(reader).lines();
    while let Some(line) = lines.next_line().await? {
        if let Ok(event) = parse_event_line(&line) {
            events.push(event);
        }
    }
    Ok(events)
}

async fn drain_sidecar_stderr<R: tokio::io::AsyncRead + Unpin>(
    reader: R,
    request_id: &str,
) -> anyhow::Result<StderrSummary> {
    let show_raw = raw_sidecar_logs_enabled();
    let mut summary = StderrSummary::default();
    let mut lines = BufReader::new(reader).lines();
    while let Some(line) = lines.next_line().await? {
        parse_summary_line(&mut summary, &line);
        if show_raw || looks_like_warning_or_error(&line) {
            eprintln!("desktop.sidecar.stderr request_id={request_id} line={line}");
            continue;
        }
        if let Some(bucket) = noisy_pattern_bucket(&line) {
            summary.noisy_counts[bucket] += 1;
            continue;
        }
        if line.trim().is_empty() {
            continue;
        }
        eprintln!("desktop.sidecar.stderr request_id={request_id} line={line}");
    }
    if !show_raw {
        eprintln!(
            "desktop.sidecar.stderr_summary request_id={} metadata_dumps={} eog_spam={} layer_spam={} repack_spam={}",
            request_id,
            summary.noisy_counts[0],
            summary.noisy_counts[1],
            summary.noisy_counts[2],
            summary.noisy_counts[3],
        );
    }
    Ok(summary)
}

fn build_sidecar_command(
    sidecar_path: &str,
    launcher: Option<PythonLauncher>,
) -> anyhow::Result<Command> {
    if is_python_script(sidecar_path) {
        let launcher = launcher.ok_or_else(|| {
            anyhow::anyhow!("missing resolved Python launcher for sidecar script")
        })?;
        return Ok(launcher.command_for_script(sidecar_path));
    }

    Ok(Command::new(sidecar_path))
}

fn is_python_script(path: &str) -> bool {
    Path::new(path)
        .extension()
        .and_then(|ext| ext.to_str())
        .is_some_and(|ext| ext.eq_ignore_ascii_case("py"))
}

fn default_sidecar_script_candidates(
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
                    .join("inference_sidecar.py"),
            );
            candidates.push(exe_dir.join("python").join("inference_sidecar.py"));
            candidates.push(
                exe_dir
                    .join("resources")
                    .join("python")
                    .join("fake_llama_sidecar.py"),
            );
            candidates.push(exe_dir.join("inference_sidecar.py"));
            candidates.push(exe_dir.join("fake_llama_sidecar.py"));

            if let Some(parent_dir) = exe_dir.parent() {
                candidates.push(
                    parent_dir
                        .join("Resources")
                        .join("python")
                        .join("inference_sidecar.py"),
                );
                candidates.push(
                    parent_dir
                        .join("Resources")
                        .join("python")
                        .join("fake_llama_sidecar.py"),
                );
                candidates.push(
                    parent_dir
                        .join("resources")
                        .join("python")
                        .join("inference_sidecar.py"),
                );
                candidates.push(
                    parent_dir
                        .join("resources")
                        .join("python")
                        .join("fake_llama_sidecar.py"),
                );
            }
        }
    }

    candidates.push(manifest_dir.join("python").join("inference_sidecar.py"));
    candidates.push(
        manifest_dir
            .join("..")
            .join("sidecar")
            .join("fake_llama_sidecar.py"),
    );
    candidates
}

fn resolve_default_sidecar_script() -> String {
    let exe_path = std::env::current_exe().ok();
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let candidates = default_sidecar_script_candidates(exe_path.as_deref(), manifest_dir);

    if let Some(path) = first_existing_script(candidates) {
        return path;
    }

    "../sidecar/fake_llama_sidecar.py".into()
}

fn first_existing_script(candidates: Vec<std::path::PathBuf>) -> Option<String> {
    candidates
        .into_iter()
        .find(|candidate| candidate.is_file())
        .map(|candidate| candidate.to_string_lossy().into_owned())
}

fn should_force_fake_sidecar() -> bool {
    matches!(
        std::env::var("TOKEN_PLACE_USE_FAKE_SIDECAR").as_deref(),
        Ok("1")
    )
}

fn configure_runtime_pythonpath(command: &mut Command, sidecar_path: &str) {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    if let Some(import_root) =
        resolve_runtime_import_root(Some(Path::new(sidecar_path)), manifest_dir)
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

pub async fn start_sidecar(
    app: AppHandle,
    state: SidecarState,
    request: InferenceRequest,
) -> anyhow::Result<()> {
    {
        let mut child_slot = state.child.lock().await;
        if child_slot
            .as_mut()
            .is_some_and(|child| child.try_wait().ok().flatten().is_none())
        {
            anyhow::bail!("inference already running; cancel before starting a new request");
        }
        *child_slot = None;
        *state.stdin.lock().await = None;
    }

    let sidecar_script = std::env::var("TOKEN_PLACE_SIDECAR").unwrap_or_else(|_| {
        if should_force_fake_sidecar() {
            "../sidecar/fake_llama_sidecar.py".into()
        } else {
            resolve_default_sidecar_script()
        }
    });

    let launcher = if is_python_script(&sidecar_script) {
        Some(
            tokio::task::spawn_blocking(|| resolve_python_launcher("TOKEN_PLACE_SIDECAR_PYTHON"))
                .await
                .map_err(|e| anyhow::anyhow!("python launcher resolver task failed: {e}"))??,
        )
    } else {
        None
    };

    let mut sidecar_command = build_sidecar_command(&sidecar_script, launcher)?;
    configure_runtime_pythonpath(&mut sidecar_command, &sidecar_script);

    let mut child = sidecar_command
        .arg("--model")
        .arg(&request.model_path)
        .arg("--mode")
        .arg(format!("{:?}", request.mode).to_lowercase())
        .arg("--prompt")
        .arg(&request.prompt)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing sidecar stdout"))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing sidecar stderr"))?;
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("missing sidecar stdin"))?;

    {
        let mut child_slot = state.child.lock().await;
        *child_slot = Some(child);
        let mut stdin_slot = state.stdin.lock().await;
        *stdin_slot = Some(stdin);
    }

    let request_id = request.request_id;
    let stderr_request_id = request_id.clone();
    let stderr_model_path = request.model_path.clone();
    let stderr_mode = format!("{:?}", request.mode).to_lowercase();
    let stderr_task = tokio::spawn(async move {
        let summary = drain_sidecar_stderr(stderr, &stderr_request_id).await;
        (stderr_request_id, stderr_model_path, stderr_mode, summary)
    });

    let mut reader = BufReader::new(stdout).lines();
    let mut saw_error_event = false;
    while let Some(line) = reader.next_line().await? {
        match parse_event_line(&line) {
            Ok(event) => {
                if matches!(event, SidecarEvent::Error { .. }) {
                    saw_error_event = true;
                }
                app.emit(
                    "inference_event",
                    UiInferenceEvent {
                        request_id: request_id.clone(),
                        event,
                    },
                )?;
            }
            Err(err) => {
                eprintln!(
                    "desktop.sidecar.stdout_parse_error request_id={} error={} line={}",
                    request_id, err, line
                );
            }
        }
    }

    match stderr_task.await {
        Ok((stderr_request_id, model_path, mode, Ok(summary))) => {
            if !raw_sidecar_logs_enabled() {
                eprintln!(
                    "desktop.sidecar.runtime_summary request_id={} model_path={} mode={} backend={} device={} context={} offload={} load_time={} prompt_tps={} eval_tps={} fallback_reason={}",
                    stderr_request_id,
                    model_path,
                    mode,
                    summary.backend.as_deref().unwrap_or("n/a"),
                    summary.device.as_deref().unwrap_or("n/a"),
                    summary.context_size.as_deref().unwrap_or("n/a"),
                    summary.offloaded_layers.as_deref().unwrap_or("n/a"),
                    summary.load_time.as_deref().unwrap_or("n/a"),
                    summary.prompt_throughput.as_deref().unwrap_or("n/a"),
                    summary.eval_throughput.as_deref().unwrap_or("n/a"),
                    summary.fallback_reason.as_deref().unwrap_or("n/a"),
                );
            }
        }
        Ok((stderr_request_id, _, _, Err(err))) => {
            eprintln!(
                "desktop.sidecar.stderr_error request_id={} error={}",
                stderr_request_id, err
            );
        }
        Err(err) => {
            eprintln!(
                "desktop.sidecar.stderr_task_join_error request_id={} error={}",
                request_id, err
            );
        }
    }

    let running_child = {
        let mut child_slot = state.child.lock().await;
        child_slot.take()
    };

    if let Some(mut running_child) = running_child {
        let exit_status = running_child.wait().await?;

        if !exit_status.success() && !saw_error_event {
            app.emit(
                "inference_event",
                UiInferenceEvent {
                    request_id: request_id.clone(),
                    event: SidecarEvent::Error {
                        code: "sidecar_exit".into(),
                        message: format!(
                            "sidecar exited with status {exit_status}; see desktop.sidecar.stderr logs"
                        ),
                    },
                },
            )?;
        }
    }

    *state.stdin.lock().await = None;
    Ok(())
}

pub async fn cancel_sidecar(state: SidecarState) -> anyhow::Result<()> {
    if let Some(stdin) = state.stdin.lock().await.as_mut() {
        stdin.write_all(b"{\"type\":\"cancel\"}\n").await?;
        stdin.flush().await?;
    }

    let mut child_lock = state.child.lock().await;
    if let Some(child) = child_lock.as_mut() {
        for _ in 0..10 {
            if child.try_wait()?.is_some() {
                *child_lock = None;
                *state.stdin.lock().await = None;
                return Ok(());
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        let _ = child.kill().await;
    }

    *child_lock = None;
    *state.stdin.lock().await = None;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::{NamedTempFile, TempDir};
    use tokio::process::Command;

    #[test]
    fn parses_token_event() {
        let event = parse_event_line(r#"{"type":"token","text":"hi"}"#).expect("parse");
        assert_eq!(event, SidecarEvent::Token { text: "hi".into() });
    }

    #[test]
    fn maps_error_event() {
        let event = parse_event_line(r#"{"type":"error","code":"bad_model","message":"no file"}"#)
            .expect("parse");
        assert_eq!(
            event,
            SidecarEvent::Error {
                code: "bad_model".into(),
                message: "no file".into()
            }
        );
    }

    #[tokio::test]
    async fn fake_sidecar_happy_path() {
        let model = NamedTempFile::new().expect("tempfile");
        let mut child = Command::new("python3")
            .arg("../sidecar/fake_llama_sidecar.py")
            .arg("--model")
            .arg(model.path())
            .arg("--mode")
            .arg("cpu")
            .arg("--prompt")
            .arg("hello world")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn fake sidecar");

        let stdout = child.stdout.take().expect("stdout");
        let events = collect_events_from_stdout(stdout)
            .await
            .expect("collect events");
        assert!(events.iter().any(|e| matches!(e, SidecarEvent::Started)));
        assert!(events.iter().any(|e| matches!(e, SidecarEvent::Done)));
    }

    #[tokio::test]
    async fn real_bridge_happy_path_with_mock_runtime() {
        let model = NamedTempFile::new().expect("tempfile");
        let sidecar_script = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("python")
            .join("inference_sidecar.py");

        let mut child = Command::new("python3")
            .arg(sidecar_script)
            .arg("--model")
            .arg(model.path())
            .arg("--mode")
            .arg("cpu")
            .arg("--prompt")
            .arg("hello world")
            .env("USE_MOCK_LLM", "1")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .spawn()
            .expect("spawn bridge sidecar");

        let stdout = child.stdout.take().expect("stdout");
        let events = collect_events_from_stdout(stdout)
            .await
            .expect("collect events");
        assert!(events.iter().any(|e| matches!(e, SidecarEvent::Started)));
        assert!(events
            .iter()
            .any(|e| matches!(e, SidecarEvent::Token { .. })));
        assert!(events.iter().any(|e| matches!(e, SidecarEvent::Done)));
    }

    #[tokio::test]
    async fn drain_sidecar_stderr_reads_all_lines() {
        let script = NamedTempFile::new().expect("temp script");
        std::fs::write(
            script.path(),
            "#!/usr/bin/env python3\nimport sys\nprint('first', file=sys.stderr)\nprint('second', file=sys.stderr)\n",
        )
        .expect("write script");

        let mut child = Command::new("python3")
            .arg(script.path())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn stderr script");

        let stderr = child.stderr.take().expect("stderr");
        drain_sidecar_stderr(stderr, "test")
            .await
            .expect("drain stderr");
        let status = child.wait().await.expect("wait child");
        assert!(status.success());
    }

    #[tokio::test]
    async fn collect_events_ignores_malformed_stdout_lines_and_keeps_valid_flow() {
        let stdout = b"{\"type\":\"started\"}\nnot-json\n{\"type\":\"token\",\"text\":\"ok\"}\n{\"type\":\"done\"}\n"
            .as_slice();
        let events = collect_events_from_stdout(stdout)
            .await
            .expect("collect events");
        assert_eq!(
            events,
            vec![
                SidecarEvent::Started,
                SidecarEvent::Token { text: "ok".into() },
                SidecarEvent::Done
            ]
        );
    }

    #[test]
    fn sidecar_candidates_include_packaged_resource_locations() {
        let temp = TempDir::new().expect("tempdir");
        let app_root = temp.path().join("Token Place.app");
        let exe_dir = app_root.join("Contents").join("MacOS");
        let exe_path = exe_dir.join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");
        let candidates = default_sidecar_script_candidates(Some(&exe_path), &manifest_dir);

        assert!(candidates
            .iter()
            .any(|candidate| candidate.ends_with("resources/python/inference_sidecar.py")));
        assert!(candidates
            .iter()
            .any(|candidate| candidate.ends_with("Resources/python/inference_sidecar.py")));
        assert!(candidates.iter().any(
            |candidate| candidate == &manifest_dir.join("python").join("inference_sidecar.py")
        ));
    }

    #[test]
    fn first_existing_script_finds_packaged_resource_sidecar_path() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");
        let sidecar = resources_dir.join("inference_sidecar.py");
        std::fs::write(&sidecar, "print('ok')\n").expect("write sidecar");

        let exe_path = exe_dir.join("token.place.exe");
        let candidates = default_sidecar_script_candidates(Some(&exe_path), temp.path());
        let resolved = first_existing_script(candidates).expect("resolved sidecar path");

        assert_eq!(Path::new(&resolved), sidecar);
    }

    #[test]
    fn first_existing_script_prefers_resources_over_exe_python_sidecar_path() {
        let temp = TempDir::new().expect("tempdir");
        let exe_dir = temp.path().join("bin");
        let exe_python_dir = exe_dir.join("python");
        let resources_dir = exe_dir.join("resources").join("python");
        std::fs::create_dir_all(&exe_python_dir).expect("create exe python dir");
        std::fs::create_dir_all(&resources_dir).expect("create resources dir");

        let exe_sidecar = exe_python_dir.join("inference_sidecar.py");
        std::fs::write(&exe_sidecar, "print('exe')\n").expect("write exe sidecar");
        let resources_sidecar = resources_dir.join("inference_sidecar.py");
        std::fs::write(&resources_sidecar, "print('resources')\n")
            .expect("write resources sidecar");

        let exe_path = exe_dir.join("token.place");
        let candidates = default_sidecar_script_candidates(Some(&exe_path), temp.path());
        let resolved = first_existing_script(candidates).expect("resolved sidecar path");

        assert_eq!(Path::new(&resolved), resources_sidecar);
    }
}
