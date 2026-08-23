use crate::compute_node::prepare_operator_bridge_launch;
use crate::python_runtime::{BridgeResourceContext, PythonLauncherSource};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

const SCHEMA: &str = "token.place.desktop.headless-cpu-admission/v1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Arguments {
    model: PathBuf,
    context_tier: String,
    startup_timeout: Duration,
    operation_timeout: Duration,
}

#[derive(Debug, Deserialize)]
struct BridgeResult {
    success: bool,
    last_completed_phase: String,
    failure_code: String,
    warm_load_result: String,
    authoritative_evidence_result: String,
}

#[derive(Debug, Serialize)]
pub(crate) struct ResultRecord {
    schema_version: &'static str,
    pub(crate) success: bool,
    last_completed_phase: String,
    failure_code: String,
    packaged_runtime_identity_result: &'static str,
    selected_backend: &'static str,
    warm_load_result: String,
    authoritative_evidence_result: String,
}

impl ResultRecord {
    pub(crate) fn failed(phase: &str, code: &str) -> Self {
        Self {
            schema_version: SCHEMA,
            success: false,
            last_completed_phase: phase.into(),
            failure_code: code.into(),
            packaged_runtime_identity_result: if phase == "arguments_validated" {
                "not_started"
            } else {
                "failed"
            },
            selected_backend: "cpu",
            warm_load_result: "not_started".into(),
            authoritative_evidence_result: "not_started".into(),
        }
    }
}

pub(crate) fn parse(args: &[String]) -> Result<Option<Arguments>, &'static str> {
    let Some(index) = args
        .iter()
        .position(|arg| arg == "--headless-cpu-admission")
    else {
        return Ok(None);
    };
    if index != 1 {
        return Err("invalid_arguments");
    }
    let (mut model, mut backend, mut tier, mut startup, mut operation) =
        (None, None, None, None, None);
    let mut cursor = 2;
    while cursor < args.len() {
        let value = args.get(cursor + 1).ok_or("invalid_arguments")?;
        let slot = match args[cursor].as_str() {
            "--model" => &mut model,
            "--backend" => &mut backend,
            "--context-tier" => &mut tier,
            "--startup-timeout-seconds" => &mut startup,
            "--operation-timeout-seconds" => &mut operation,
            _ => return Err("invalid_arguments"),
        };
        if slot.replace(value.clone()).is_some() || value.is_empty() {
            return Err("invalid_arguments");
        }
        cursor += 2;
    }
    let model = PathBuf::from(model.ok_or("invalid_arguments")?);
    if !model.is_absolute() || backend.as_deref() != Some("cpu") {
        return Err("invalid_arguments");
    }
    let context_tier = tier.ok_or("invalid_arguments")?;
    if !matches!(
        context_tier.as_str(),
        "8k-fast" | "32k-balanced" | "64k-full"
    ) {
        return Err("invalid_arguments");
    }
    fn seconds(value: Option<String>) -> Result<Duration, &'static str> {
        let value = value
            .ok_or("invalid_arguments")?
            .parse::<u64>()
            .map_err(|_| "invalid_arguments")?;
        (1..=3600)
            .contains(&value)
            .then(|| Duration::from_secs(value))
            .ok_or("invalid_arguments")
    }
    Ok(Some(Arguments {
        model,
        context_tier,
        startup_timeout: seconds(startup)?,
        operation_timeout: seconds(operation)?,
    }))
}

pub(crate) fn execute(arguments: Arguments) -> ResultRecord {
    match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime.block_on(execute_async(arguments)),
        Err(_) => ResultRecord::failed("arguments_validated", "orchestrator_start_failed"),
    }
}

async fn execute_async(arguments: Arguments) -> ResultRecord {
    let current_exe = std::env::current_exe().ok();
    let context = BridgeResourceContext {
        exe_path: current_exe.as_deref(),
        manifest_dir: Path::new(env!("CARGO_MANIFEST_DIR")),
        tauri_resource_dir: None,
    };
    let preparation = match prepare_operator_bridge_launch(&context) {
        Ok(value) => value,
        Err(_) => {
            return ResultRecord::failed("arguments_validated", "packaged_runtime_identity_failed")
        }
    };
    if !context.packaged()
        || !matches!(
            preparation.launcher.as_ref().map(|v| &v.source),
            Some(PythonLauncherSource::BundledRuntime)
        )
    {
        return ResultRecord::failed("arguments_validated", "packaged_runtime_identity_failed");
    }
    let mut command = match preparation.command() {
        Ok(value) => value,
        Err(_) => {
            return ResultRecord::failed("arguments_validated", "packaged_runtime_identity_failed")
        }
    };
    command
        .args(["--headless-cpu-admission", "--model"])
        .arg(&arguments.model)
        .args(["--mode", "cpu", "--context-tier", &arguments.context_tier])
        .env(
            "TOKENPLACE_API_V1_WARM_LOAD_WAIT_SECONDS",
            arguments.operation_timeout.as_secs().to_string(),
        )
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let child = match command.spawn() {
        Ok(child) => child,
        Err(_) => return ResultRecord::failed("packaged_runtime_identity", "bridge_spawn_failed"),
    };
    let deadline = arguments
        .startup_timeout
        .saturating_add(arguments.operation_timeout);
    let output = match tokio::time::timeout(deadline, child.wait_with_output()).await {
        Ok(Ok(output)) => output,
        Ok(Err(_)) => return ResultRecord::failed("packaged_runtime_identity", "bridge_io_failed"),
        Err(_) => return ResultRecord::failed("packaged_runtime_identity", "operation_timeout"),
    };
    let parsed = String::from_utf8_lossy(&output.stdout)
        .lines()
        .rev()
        .find_map(|line| serde_json::from_str::<BridgeResult>(line).ok());
    let Some(parsed) = parsed else {
        return ResultRecord::failed(
            "packaged_runtime_identity",
            "bridge_exited_before_startup_event",
        );
    };
    let success = output.status.success()
        && parsed.success
        && parsed.authoritative_evidence_result == "passed";
    ResultRecord {
        schema_version: SCHEMA,
        success,
        last_completed_phase: parsed.last_completed_phase,
        failure_code: if success {
            "none".into()
        } else {
            parsed.failure_code
        },
        packaged_runtime_identity_result: "passed",
        selected_backend: "cpu",
        warm_load_result: parsed.warm_load_result,
        authoritative_evidence_result: parsed.authoritative_evidence_result,
    }
}

pub(crate) fn print(record: &ResultRecord) {
    println!(
        "{}",
        serde_json::to_string(record).expect("result serialization")
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn absent_command_preserves_normal_startup() {
        assert_eq!(parse(&["app".into()]), Ok(None));
    }
    #[test]
    fn validation_fails_closed() {
        assert_eq!(
            parse(&["app".into(), "--headless-cpu-admission".into()]),
            Err("invalid_arguments")
        );
        let args = vec![
            "app",
            "--headless-cpu-admission",
            "--model",
            "/model.gguf",
            "--backend",
            "gpu",
            "--context-tier",
            "8k-fast",
            "--startup-timeout-seconds",
            "1",
            "--operation-timeout-seconds",
            "1",
        ]
        .into_iter()
        .map(String::from)
        .collect::<Vec<_>>();
        assert_eq!(parse(&args), Err("invalid_arguments"));
    }
    #[test]
    fn result_is_privacy_safe_and_deterministic() {
        let json =
            serde_json::to_string(&ResultRecord::failed("warm_load", "warm_load_failed")).unwrap();
        assert!(!json.contains("model.gguf"));
        assert!(json.contains("\"failure_code\":\"warm_load_failed\""));
    }
}
