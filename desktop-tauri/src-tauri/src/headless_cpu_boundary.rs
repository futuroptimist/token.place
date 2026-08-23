use crate::{build_identity, compute_node, python_runtime};
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, BufReader};

const COMMAND: &str = "--headless-cpu-admission";

#[derive(Debug, PartialEq, Eq)]
struct Args {
    model: PathBuf,
    context_tier: String,
    startup_timeout: u64,
    operation_timeout: u64,
}
#[derive(Serialize)]
struct FailureResult<'a> {
    schema_version: u8,
    success: bool,
    last_completed_phase: &'a str,
    failure_code: &'a str,
    packaged_runtime_identity: &'a str,
    selected_backend: &'a str,
    warm_load_result: &'a str,
    authoritative_evidence_result: &'a str,
}

pub(crate) fn requested<I, S>(args: I) -> bool
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    args.into_iter().any(|arg| arg.as_ref() == COMMAND)
}

fn parse<I, S>(args: I) -> Result<Args, &'static str>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let values: Vec<String> = args.into_iter().map(|v| v.as_ref().to_owned()).collect();
    if values.first().map(String::as_str) != Some(COMMAND) {
        return Err("command_not_first");
    }
    if values.len() != 11 {
        return Err("invalid_arguments");
    }
    let mut model = None;
    let mut backend = None;
    let mut tier = None;
    let mut startup = None;
    let mut operation = None;
    for pair in values[1..].chunks_exact(2) {
        if pair[1].starts_with("--") {
            return Err("invalid_arguments");
        }
        let slot = match pair[0].as_str() {
            "--model" => &mut model,
            "--backend" => &mut backend,
            "--context-tier" => &mut tier,
            "--startup-timeout-seconds" => &mut startup,
            "--operation-timeout-seconds" => &mut operation,
            _ => return Err("unknown_argument"),
        };
        if slot.replace(pair[1].clone()).is_some() {
            return Err("duplicate_argument");
        }
    }
    if backend.as_deref() != Some("cpu") {
        return Err("unsupported_backend");
    }
    let context_tier = tier.ok_or("invalid_arguments")?;
    if !matches!(context_tier.as_str(), "8k-fast" | "64k-full") {
        return Err("unsupported_context_tier");
    }
    let timeout = |value: Option<String>| {
        value
            .and_then(|v| v.parse::<u64>().ok())
            .filter(|v| (1..=3600).contains(v))
            .ok_or("invalid_timeout")
    };
    let model = PathBuf::from(model.ok_or("invalid_arguments")?);
    if !model.is_file() {
        return Err("unusable_model_path");
    }
    Ok(Args {
        model,
        context_tier,
        startup_timeout: timeout(startup)?,
        operation_timeout: timeout(operation)?,
    })
}

fn failure(code: &'static str, phase: &'static str) -> String {
    serde_json::to_string(&FailureResult {
        schema_version: 1,
        success: false,
        last_completed_phase: phase,
        failure_code: code,
        packaged_runtime_identity: "failed",
        selected_backend: "cpu",
        warm_load_result: "not_started",
        authoritative_evidence_result: "failed",
    })
    .unwrap()
}

async fn cleanup(child: &mut tokio::process::Child) -> bool {
    let Some(pid) = child.id() else {
        return child.wait().await.is_ok();
    };
    let tree_stopped = compute_node::terminate_bridge_process_tree(pid).await;
    let reaped = tokio::time::timeout(Duration::from_secs(2), child.wait())
        .await
        .is_ok();
    tree_stopped && reaped
}

pub(crate) fn run(argv: Vec<String>) -> i32 {
    let args = match parse(argv.iter().skip(1)) {
        Ok(v) => v,
        Err(code) => {
            println!("{}", failure(code, "not_started"));
            return 2;
        }
    };
    let exe = std::env::current_exe().ok();
    let context = python_runtime::BridgeResourceContext {
        exe_path: exe.as_deref(),
        manifest_dir: Path::new(env!("CARGO_MANIFEST_DIR")),
        tauri_resource_dir: None,
    };
    if !context.packaged() {
        println!(
            "{}",
            failure("installed_package_required", "arguments_validated")
        );
        return 3;
    }
    let preparation = match compute_node::prepare_operator_bridge_launch(&context) {
        Ok(v) => v,
        Err(_) => {
            println!(
                "{}",
                failure("packaged_runtime_identity_failed", "arguments_validated")
            );
            return 3;
        }
    };
    let launcher = match preparation.launcher.as_ref() {
        Some(v) if v.source == python_runtime::PythonLauncherSource::BundledRuntime => v,
        _ => {
            println!(
                "{}",
                failure("packaged_runtime_identity_failed", "arguments_validated")
            );
            return 3;
        }
    };
    let mut command = match preparation.command() {
        Ok(v) => v,
        Err(_) => {
            println!(
                "{}",
                failure("packaged_runtime_identity_failed", "arguments_validated")
            );
            return 3;
        }
    };
    let identity = build_identity::build_identity();
    command
        .args([COMMAND, "--model"])
        .arg(args.model)
        .args(["--mode", "cpu", "--context-tier", &args.context_tier])
        .env("TOKENPLACE_APP_VERSION", identity.app_version)
        .env("TOKENPLACE_BUILD_ID", identity.build_id)
        .env("TOKENPLACE_TARGET_TRIPLE", identity.target_triple)
        .env("TOKENPLACE_BUNDLED_RUNTIME_ID", identity.bundled_runtime_id)
        .env("TOKENPLACE_LAUNCHER_SOURCE", "bundled_runtime")
        .env("TOKENPLACE_RUNTIME_ID", &launcher.runtime_id)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    compute_node::isolate_bridge_process_tree(&mut command);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(async move {
        let mut child = match command.spawn() { Ok(v) => v, Err(_) => { println!("{}", failure("bridge_exited_before_startup_event", "arguments_validated")); return 7; } };
        let stdout = child.stdout.take().unwrap(); let mut lines = BufReader::new(stdout).lines();
        let startup = tokio::time::timeout(Duration::from_secs(args.startup_timeout), lines.next_line()).await;
        let startup_ok = matches!(startup, Ok(Ok(Some(ref line))) if serde_json::from_str::<Value>(line).ok().is_some_and(|v| v.get("type")==Some(&Value::from("headless_internal")) && v.get("phase")==Some(&Value::from("startup_ready"))));
        if !startup_ok { let ok=cleanup(&mut child).await; println!("{}", failure(if ok {"bridge_exited_before_startup_event"} else {"cleanup_failed"}, "arguments_validated")); return if ok {7} else {8}; }
        let final_line = tokio::time::timeout(Duration::from_secs(args.operation_timeout), lines.next_line()).await;
        let line = match final_line { Ok(Ok(Some(v))) => v, _ => { let ok=cleanup(&mut child).await; println!("{}", failure(if ok {"operation_timeout"} else {"cleanup_failed"}, "runtime_identity_validated")); return if ok {9} else {8}; } };
        let pid = child.id();
        let status = tokio::time::timeout(Duration::from_secs(2), child.wait()).await;
        let parsed = serde_json::from_str::<Value>(&line).ok();
        let valid = parsed.as_ref().is_some_and(|v| v.get("schema_version")==Some(&Value::from(1)) && v.get("selected_backend")==Some(&Value::from("cpu")));
        if !valid || status.is_err() { let ok=cleanup(&mut child).await; println!("{}", failure(if ok {"bridge_exited_before_startup_event"} else {"cleanup_failed"}, "runtime_identity_validated")); return if ok {7} else {8}; }
        let status_success = status.as_ref().ok().and_then(|result| result.as_ref().ok()).is_some_and(|value| value.success());
        if !status_success {
            let cleaned = match pid { Some(pid) => compute_node::terminate_bridge_process_tree(pid).await, None => false };
            if !cleaned { println!("{}", failure("cleanup_failed", "runtime_identity_validated")); return 8; }
        }
        println!("{line}");
        let complete = parsed.as_ref().is_some_and(|v| v.get("success")==Some(&Value::Bool(true)) && v.get("last_completed_phase")==Some(&Value::from("cleanup_completed")) && v.get("failure_code")==Some(&Value::from("none")) && v.get("packaged_runtime_identity")==Some(&Value::from("validated")) && v.get("warm_load_result")==Some(&Value::from("ready")) && v.get("authoritative_evidence_result")==Some(&Value::from("validated")));
        if status_success && complete { 0 } else { 7 }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::NamedTempFile;
    #[test]
    fn dispatch_and_validation_are_fail_closed() {
        let model = NamedTempFile::new().unwrap();
        let m = model.path().to_str().unwrap();
        assert!(requested(["x", COMMAND]));
        assert_eq!(
            parse(["--other", COMMAND]).unwrap_err(),
            "command_not_first"
        );
        assert_eq!(
            parse([
                COMMAND,
                "--model",
                m,
                "--backend",
                "gpu",
                "--context-tier",
                "8k-fast",
                "--startup-timeout-seconds",
                "1",
                "--operation-timeout-seconds",
                "1"
            ])
            .unwrap_err(),
            "unsupported_backend"
        );
        assert_eq!(
            parse([
                COMMAND,
                "--model",
                m,
                "--model",
                m,
                "--context-tier",
                "8k-fast",
                "--startup-timeout-seconds",
                "1",
                "--operation-timeout-seconds",
                "1"
            ])
            .unwrap_err(),
            "duplicate_argument"
        );
    }
    #[test]
    fn stable_failure_is_privacy_safe() {
        let output = failure("warm_load_failed", "runtime_identity_validated");
        assert!(!output.contains("model_path"));
        assert!(!output.contains("prompt"));
    }
}
