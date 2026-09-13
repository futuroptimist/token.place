use crate::{build_identity, compute_node, python_runtime};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, BufReader};

const COMMAND: &str = "--installed-gpu-completion-preflight";

#[derive(Debug, PartialEq, Eq)]
struct Args {
    model: PathBuf,
    backend: String,
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
    if !matches!(backend.as_deref(), Some("cuda" | "metal")) {
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
        backend: backend.expect("validated backend"),
        context_tier,
        startup_timeout: timeout(startup)?,
        operation_timeout: timeout(operation)?,
    })
}

#[derive(Clone, Copy)]
struct FailureState {
    phase: &'static str,
    identity_validated: bool,
}

fn failure(code: &'static str, state: FailureState) -> String {
    serde_json::to_string(&FailureResult {
        schema_version: 1,
        success: false,
        last_completed_phase: state.phase,
        failure_code: code,
        packaged_runtime_identity: if state.identity_validated {
            "validated"
        } else {
            "failed"
        },
        selected_backend: "unknown",
        warm_load_result: "not_started",
        authoritative_evidence_result: "failed",
    })
    .unwrap()
}

async fn cleanup(
    child: &mut tokio::process::Child,
    containment: &compute_node::BridgeProcessContainment,
) -> bool {
    containment.terminate_and_reap(child).await
}

struct SupervisionResult {
    output: String,
    exit_code: i32,
}

struct TerminalResult {
    success: bool,
    failure_code: String,
}

fn validate_terminal(line: &str) -> Option<TerminalResult> {
    let value: Value = serde_json::from_str(line).ok()?;
    let object = value.as_object()?;
    let allowed = [
        "schema_version",
        "success",
        "last_completed_phase",
        "failure_code",
        "artifact_identity",
        "model_identity",
        "declared_backend",
        "observed_backend",
        "gpu_execution_observed",
        "completion_count",
        "output_validation",
        "output_max_tokens",
        "phase_timings_ms",
        "cleanup_status",
    ];
    if object.keys().any(|key| !allowed.contains(&key.as_str()))
        || value.get("schema_version")?.as_str()? != "installed-gpu-completion-preflight-v1"
    {
        return None;
    }
    let success = value.get("success")?.as_bool()?;
    let failure_code = value.get("failure_code")?.as_str()?.to_owned();
    let coherent = value.get("last_completed_phase")?.as_str()? == "cleanup_completed"
        && failure_code == "none"
        && value.get("cleanup_status")?.as_str()? == "verified"
        && value.get("completion_count")?.as_u64()? == 1
        && value.get("gpu_execution_observed")?.as_bool()?;
    if success != coherent {
        return None;
    }
    Some(TerminalResult {
        success,
        failure_code,
    })
}

async fn confirm_terminal(
    child: &mut tokio::process::Child,
    containment: &compute_node::BridgeProcessContainment,
    line: String,
    result: TerminalResult,
    failure_state: FailureState,
) -> SupervisionResult {
    if result.failure_code == "cleanup_failed" {
        let cleaned = cleanup(child, containment).await;
        return SupervisionResult {
            output: line,
            exit_code: if cleaned { 7 } else { 8 },
        };
    }
    let root_pid = child.id();
    match tokio::time::timeout(Duration::from_secs(2), child.wait()).await {
        Ok(Ok(status)) if status.success() == result.success => SupervisionResult {
            output: line,
            exit_code: if result.success { 0 } else { 7 },
        },
        status => {
            let cleaned = match (status, root_pid) {
                (Ok(Ok(_)), Some(_)) => containment.terminate_and_reap(child).await,
                (Ok(Ok(_)), None) => false,
                (Ok(Err(_)) | Err(_), _) => cleanup(child, containment).await,
            };
            SupervisionResult {
                output: failure(
                    if cleaned {
                        "bridge_protocol_failed"
                    } else {
                        "cleanup_failed"
                    },
                    failure_state,
                ),
                exit_code: if cleaned { 7 } else { 8 },
            }
        }
    }
}

async fn supervise_bridge(
    child: &mut tokio::process::Child,
    containment: &compute_node::BridgeProcessContainment,
    stdout: tokio::process::ChildStdout,
    startup_timeout: Duration,
    operation_timeout: Duration,
) -> SupervisionResult {
    let before_startup = FailureState {
        phase: "arguments_validated",
        identity_validated: false,
    };
    let after_startup = FailureState {
        phase: "runtime_identity_validated",
        identity_validated: true,
    };
    let mut lines = BufReader::new(stdout).lines();
    let startup = tokio::time::timeout(startup_timeout, lines.next_line()).await;
    let startup_line = match startup {
        Err(_) => {
            let cleaned = cleanup(child, containment).await;
            return SupervisionResult {
                output: failure(
                    if cleaned {
                        "startup_timeout"
                    } else {
                        "cleanup_failed"
                    },
                    before_startup,
                ),
                exit_code: if cleaned { 9 } else { 8 },
            };
        }
        Ok(Ok(Some(line))) => line,
        Ok(Ok(None)) | Ok(Err(_)) => {
            let cleaned = cleanup(child, containment).await;
            return SupervisionResult {
                output: failure(
                    if cleaned {
                        "bridge_exited_before_startup_event"
                    } else {
                        "cleanup_failed"
                    },
                    before_startup,
                ),
                exit_code: if cleaned { 7 } else { 8 },
            };
        }
    };
    let startup_valid = serde_json::from_str::<Value>(&startup_line)
        .ok()
        .is_some_and(|value| {
            value.as_object().is_some_and(|record| {
                record.len() == 2
                    && value.get("type") == Some(&Value::from("headless_internal"))
                    && value.get("phase") == Some(&Value::from("startup_ready"))
            })
        });
    if !startup_valid {
        if let Some(result) = validate_terminal(&startup_line) {
            if !result.success {
                return confirm_terminal(child, containment, startup_line, result, before_startup)
                    .await;
            }
        }
        let cleaned = cleanup(child, containment).await;
        return SupervisionResult {
            output: failure(
                if cleaned {
                    "bridge_protocol_failed"
                } else {
                    "cleanup_failed"
                },
                before_startup,
            ),
            exit_code: if cleaned { 7 } else { 8 },
        };
    }

    let line = match tokio::time::timeout(operation_timeout, lines.next_line()).await {
        Err(_) => {
            let cleaned = cleanup(child, containment).await;
            return SupervisionResult {
                output: failure(
                    if cleaned {
                        "operation_timeout"
                    } else {
                        "cleanup_failed"
                    },
                    after_startup,
                ),
                exit_code: if cleaned { 9 } else { 8 },
            };
        }
        Ok(Ok(Some(line))) => line,
        Ok(Ok(None)) | Ok(Err(_)) => {
            let cleaned = cleanup(child, containment).await;
            return SupervisionResult {
                output: failure(
                    if cleaned {
                        "bridge_protocol_failed"
                    } else {
                        "cleanup_failed"
                    },
                    after_startup,
                ),
                exit_code: if cleaned { 7 } else { 8 },
            };
        }
    };
    let Some(parsed) = validate_terminal(&line) else {
        let cleaned = cleanup(child, containment).await;
        return SupervisionResult {
            output: failure(
                if cleaned {
                    "bridge_protocol_failed"
                } else {
                    "cleanup_failed"
                },
                after_startup,
            ),
            exit_code: if cleaned { 7 } else { 8 },
        };
    };
    confirm_terminal(child, containment, line, parsed, after_startup).await
}

pub(crate) fn run(argv: Vec<String>) -> i32 {
    let args = match parse(argv.iter().skip(1)) {
        Ok(v) => v,
        Err(code) => {
            println!(
                "{}",
                failure(
                    code,
                    FailureState {
                        phase: "not_started",
                        identity_validated: false
                    }
                )
            );
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
            failure(
                "installed_package_required",
                FailureState {
                    phase: "arguments_validated",
                    identity_validated: false
                }
            )
        );
        return 3;
    }
    let preparation = match compute_node::prepare_operator_bridge_launch(&context) {
        Ok(v) => v,
        Err(_) => {
            println!(
                "{}",
                failure(
                    "packaged_runtime_identity_failed",
                    FailureState {
                        phase: "arguments_validated",
                        identity_validated: false
                    }
                )
            );
            return 3;
        }
    };
    let launcher = match preparation.launcher.as_ref() {
        Some(v) if v.source == python_runtime::PythonLauncherSource::BundledRuntime => v,
        _ => {
            println!(
                "{}",
                failure(
                    "packaged_runtime_identity_failed",
                    FailureState {
                        phase: "arguments_validated",
                        identity_validated: false
                    }
                )
            );
            return 3;
        }
    };
    let mut command = match preparation.command() {
        Ok(v) => v,
        Err(_) => {
            println!(
                "{}",
                failure(
                    "packaged_runtime_identity_failed",
                    FailureState {
                        phase: "arguments_validated",
                        identity_validated: false
                    }
                )
            );
            return 3;
        }
    };
    let identity = build_identity::build_identity();
    command
        .args([COMMAND, "--model"])
        .arg(args.model)
        .args([
            "--mode",
            &args.backend,
            "--context-tier",
            &args.context_tier,
        ])
        .env("TOKENPLACE_APP_VERSION", identity.app_version)
        .env("TOKENPLACE_BUILD_ID", identity.build_id)
        .env("TOKENPLACE_TARGET_TRIPLE", identity.target_triple)
        .env("TOKENPLACE_BUNDLED_RUNTIME_ID", identity.bundled_runtime_id)
        .env("TOKENPLACE_LAUNCHER_SOURCE", "bundled_runtime")
        .env("TOKENPLACE_RUNTIME_ID", &launcher.runtime_id)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    let containment = match compute_node::BridgeProcessContainment::prepare(&mut command) {
        Ok(value) => value,
        Err(_) => {
            println!(
                "{}",
                failure(
                    "cleanup_failed",
                    FailureState {
                        phase: "arguments_validated",
                        identity_validated: false
                    }
                )
            );
            return 8;
        }
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(async move {
        let mut child = match command.spawn() {
            Ok(v) => v,
            Err(_) => {
                println!(
                    "{}",
                    failure(
                        "bridge_exited_before_startup_event",
                        FailureState {
                            phase: "arguments_validated",
                            identity_validated: false
                        }
                    )
                );
                return 7;
            }
        };
        if containment.assign_and_resume(&child).is_err() {
            let _ = containment.terminate_and_reap(&mut child).await;
            println!(
                "{}",
                failure(
                    "cleanup_failed",
                    FailureState {
                        phase: "arguments_validated",
                        identity_validated: false
                    }
                )
            );
            return 8;
        }
        let stdout = child.stdout.take().expect("piped bridge stdout");
        let result = supervise_bridge(
            &mut child,
            &containment,
            stdout,
            Duration::from_secs(args.startup_timeout),
            Duration::from_secs(args.operation_timeout),
        )
        .await;
        println!("{}", result.output);
        result.exit_code
    })
}
