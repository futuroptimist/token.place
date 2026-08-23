use crate::{build_identity, python_runtime};
use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::time::Duration;

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
    let all_values: Vec<String> = args.into_iter().map(|v| v.as_ref().to_owned()).collect();
    let Some(command_index) = all_values.iter().position(|value| value == COMMAND) else {
        return Err("command_missing");
    };
    let values = all_values[command_index..].to_vec();
    if values.len() != 11 {
        return Err("invalid_arguments");
    }
    let value = |name: &str| -> Result<String, &'static str> {
        let matches: Vec<_> = values
            .windows(2)
            .filter(|pair| pair[0] == name)
            .map(|pair| pair[1].clone())
            .collect();
        match matches.as_slice() {
            [only] if !only.starts_with("--") => Ok(only.clone()),
            _ => Err("invalid_arguments"),
        }
    };
    if value("--backend")?.as_str() != "cpu" {
        return Err("unsupported_backend");
    }
    let context_tier = value("--context-tier")?;
    if !matches!(context_tier.as_str(), "8k-fast" | "64k-full") {
        return Err("invalid_arguments");
    }
    let timeout = |name| {
        value(name)?
            .parse::<u64>()
            .ok()
            .filter(|seconds| (1..=3600).contains(seconds))
            .ok_or("invalid_arguments")
    };
    Ok(Args {
        model: PathBuf::from(value("--model")?),
        context_tier,
        startup_timeout: timeout("--startup-timeout-seconds")?,
        operation_timeout: timeout("--operation-timeout-seconds")?,
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
    .expect("static headless boundary result serializes")
}

pub(crate) fn run(args: Vec<String>) -> i32 {
    let args = match parse(args) {
        Ok(args) => args,
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
    let launcher = match python_runtime::resolve_python_launcher_resource_aware(
        context.launcher_options("TOKEN_PLACE_PYTHON"),
    ) {
        Ok(value) => value,
        Err(_) => {
            println!(
                "{}",
                failure("packaged_runtime_identity_failed", "arguments_validated")
            );
            return 3;
        }
    };
    let bridge = match context
        .resolve_bridge_script_path("compute_node_bridge.py", Some(&launcher.program))
    {
        Ok(value) => value,
        Err(_) => {
            println!(
                "{}",
                failure("packaged_runtime_identity_failed", "arguments_validated")
            );
            return 3;
        }
    };
    let mut command = match launcher.command_for_script(&bridge) {
        Ok(value) => value,
        Err(_) => {
            println!(
                "{}",
                failure("packaged_runtime_identity_failed", "arguments_validated")
            );
            return 3;
        }
    };
    let import_root =
        python_runtime::resolve_runtime_import_root(Some(&bridge), context.manifest_dir);
    if let Some(root) = import_root.as_deref() {
        let (_, layout) = context.describe_resource_layout(&bridge);
        python_runtime::configure_python_subprocess_env_for_layout(
            &mut command,
            root,
            layout,
            true,
        );
    }
    let identity = build_identity::build_identity();
    command
        .arg(COMMAND)
        .arg("--model")
        .arg(args.model)
        .arg("--mode")
        .arg("cpu")
        .arg("--context-tier")
        .arg(args.context_tier)
        .env("TOKENPLACE_APP_VERSION", identity.app_version)
        .env("TOKENPLACE_BUILD_ID", identity.build_id)
        .env("TOKENPLACE_TARGET_TRIPLE", identity.target_triple)
        .env("TOKENPLACE_BUNDLED_RUNTIME_ID", identity.bundled_runtime_id)
        .env("TOKENPLACE_RUNTIME_ID", launcher.runtime_id)
        .env(
            "TOKENPLACE_DESKTOP_WARM_LOAD_WAIT_SECONDS",
            args.startup_timeout.to_string(),
        )
        .kill_on_drop(true);
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let outcome = runtime.block_on(async {
        tokio::time::timeout(
            Duration::from_secs(args.operation_timeout),
            command.output(),
        )
        .await
    });
    match outcome {
        Err(_) => {
            println!(
                "{}",
                failure("operation_timeout", "runtime_identity_validated")
            );
            9
        }
        Ok(Err(_)) => {
            println!(
                "{}",
                failure("bridge_exited_before_startup_event", "arguments_validated")
            );
            7
        }
        Ok(Ok(output)) => {
            let text = String::from_utf8_lossy(&output.stdout);
            let line = text.lines().last().unwrap_or("");
            let parsed = serde_json::from_str::<Value>(line).ok();
            let valid = parsed.as_ref().is_some_and(|value| {
                value.get("schema_version") == Some(&Value::from(1))
                    && value.get("selected_backend") == Some(&Value::from("cpu"))
            });
            if valid {
                println!("{line}");
                let complete = parsed.as_ref().is_some_and(|value| {
                    value.get("success") == Some(&Value::Bool(true))
                        && value.get("last_completed_phase")
                            == Some(&Value::from("cleanup_completed"))
                        && value.get("failure_code") == Some(&Value::from("none"))
                        && value.get("packaged_runtime_identity") == Some(&Value::from("validated"))
                        && value.get("warm_load_result") == Some(&Value::from("ready"))
                        && value.get("authoritative_evidence_result")
                            == Some(&Value::from("validated"))
                });
                if output.status.success() && complete {
                    0
                } else {
                    output.status.code().filter(|code| *code != 0).unwrap_or(7)
                }
            } else {
                println!(
                    "{}",
                    failure(
                        "bridge_exited_before_startup_event",
                        "runtime_identity_validated"
                    )
                );
                7
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dispatch_and_validation_are_fail_closed() {
        assert!(requested([COMMAND]));
        assert!(!requested(["--build-identity-json"]));
        assert_eq!(parse([COMMAND]).unwrap_err(), "invalid_arguments");
        assert_eq!(
            parse([
                COMMAND,
                "--model",
                "m",
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
    }

    #[test]
    fn stable_failure_is_privacy_safe() {
        let output = failure("warm_load_failed", "runtime_identity_validated");
        assert!(!output.contains("model"));
        assert!(!output.contains("prompt"));
        assert_eq!(
            serde_json::from_str::<Value>(&output).unwrap()["failure_code"],
            "warm_load_failed"
        );
    }
}
