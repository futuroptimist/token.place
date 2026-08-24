use crate::{build_identity, compute_node, python_runtime};
use serde::{Deserialize, Serialize};
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
        selected_backend: "cpu",
        warm_load_result: "not_started",
        authoritative_evidence_result: "failed",
    })
    .unwrap()
}

async fn cleanup(child: &mut tokio::process::Child) -> bool {
    compute_node::terminate_and_reap_bridge_process_tree(child).await
}

struct SupervisionResult {
    output: String,
    exit_code: i32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TerminalResult {
    schema_version: u8,
    success: bool,
    last_completed_phase: String,
    failure_code: String,
    packaged_runtime_identity: String,
    selected_backend: String,
    warm_load_result: String,
    authoritative_evidence_result: String,
}

fn validate_terminal(line: &str) -> Option<TerminalResult> {
    let result: TerminalResult = serde_json::from_str(line).ok()?;
    let supported_failure = matches!(
        result.failure_code.as_str(),
        "none"
            | "invalid_arguments"
            | "packaged_runtime_identity_failed"
            | "unsupported_backend"
            | "mock_runtime_rejected"
            | "bridge_exited_before_startup_event"
            | "warm_load_failed"
            | "authoritative_evidence_failed"
            | "cleanup_failed"
    );
    if result.schema_version != 1
        || result.selected_backend != "cpu"
        || !matches!(
            result.last_completed_phase.as_str(),
            "arguments_validated"
                | "runtime_identity_validated"
                | "warm_load_completed"
                | "cleanup_completed"
        )
        || !matches!(
            result.packaged_runtime_identity.as_str(),
            "failed" | "validated"
        )
        || !matches!(result.warm_load_result.as_str(), "not_started" | "ready")
        || !matches!(
            result.authoritative_evidence_result.as_str(),
            "failed" | "validated"
        )
        || !supported_failure
    {
        return None;
    }
    let coherent_success = result.last_completed_phase == "cleanup_completed"
        && result.failure_code == "none"
        && result.packaged_runtime_identity == "validated"
        && result.warm_load_result == "ready"
        && result.authoritative_evidence_result == "validated";
    if result.success != coherent_success || (!result.success && result.failure_code == "none") {
        return None;
    }
    Some(result)
}

async fn confirm_terminal(
    child: &mut tokio::process::Child,
    line: String,
    result: TerminalResult,
    failure_state: FailureState,
) -> SupervisionResult {
    match tokio::time::timeout(Duration::from_secs(2), child.wait()).await {
        Ok(Ok(status)) if status.success() == result.success => SupervisionResult {
            output: line,
            exit_code: if result.success { 0 } else { 7 },
        },
        status => {
            let cleaned = match status {
                Ok(Ok(_)) => true,
                Ok(Err(_)) | Err(_) => cleanup(child).await,
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
            let cleaned = cleanup(child).await;
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
            let cleaned = cleanup(child).await;
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
                return confirm_terminal(child, startup_line, result, before_startup).await;
            }
        }
        let cleaned = cleanup(child).await;
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
            let cleaned = cleanup(child).await;
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
            let cleaned = cleanup(child).await;
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
        let cleaned = cleanup(child).await;
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
    confirm_terminal(child, line, parsed, after_startup).await
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
        let stdout = child.stdout.take().expect("piped bridge stdout");
        let result = supervise_bridge(
            &mut child,
            stdout,
            Duration::from_secs(args.startup_timeout),
            Duration::from_secs(args.operation_timeout),
        )
        .await;
        println!("{}", result.output);
        result.exit_code
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Stdio;
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
        let output = failure(
            "warm_load_failed",
            FailureState {
                phase: "runtime_identity_validated",
                identity_validated: true,
            },
        );
        assert!(!output.contains("model_path"));
        assert!(!output.contains("prompt"));
    }

    #[test]
    fn generated_failures_preserve_state_without_claiming_cleanup() {
        let timeout = failure(
            "operation_timeout",
            FailureState {
                phase: "runtime_identity_validated",
                identity_validated: true,
            },
        );
        let value: Value = serde_json::from_str(&timeout).unwrap();
        assert_eq!(value["packaged_runtime_identity"], "validated");
        assert_eq!(value["last_completed_phase"], "runtime_identity_validated");
        assert_ne!(value["last_completed_phase"], "cleanup_completed");

        let cleanup = failure(
            "cleanup_failed",
            FailureState {
                phase: "runtime_identity_validated",
                identity_validated: true,
            },
        );
        assert_ne!(
            serde_json::from_str::<Value>(&cleanup).unwrap()["last_completed_phase"],
            "cleanup_completed"
        );
    }

    #[cfg(unix)]
    fn shell_bridge(script: &str) -> (tokio::process::Child, tokio::process::ChildStdout) {
        let mut command = tokio::process::Command::new("sh");
        command
            .args(["-c", script])
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        compute_node::isolate_bridge_process_tree(&mut command);
        let mut child = command.spawn().unwrap();
        let stdout = child.stdout.take().unwrap();
        (child, stdout)
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn supervisor_distinguishes_startup_timeout_from_early_exit() {
        let (mut child, stdout) = shell_bridge("sleep 30");
        let timed_out = supervise_bridge(
            &mut child,
            stdout,
            Duration::from_millis(30),
            Duration::from_secs(1),
        )
        .await;
        assert_eq!(
            serde_json::from_str::<Value>(&timed_out.output).unwrap()["failure_code"],
            "startup_timeout"
        );

        let (mut child, stdout) = shell_bridge("exit 4");
        let exited = supervise_bridge(
            &mut child,
            stdout,
            Duration::from_secs(1),
            Duration::from_secs(1),
        )
        .await;
        assert_eq!(
            serde_json::from_str::<Value>(&exited.output).unwrap()["failure_code"],
            "bridge_exited_before_startup_event"
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn supervisor_rejects_malformed_protocol_without_forwarding_it() {
        let (mut child, stdout) = shell_bridge("printf 'private malformed payload\\n'; sleep 30");
        let result = supervise_bridge(
            &mut child,
            stdout,
            Duration::from_secs(1),
            Duration::from_secs(1),
        )
        .await;
        assert!(!result.output.contains("private malformed payload"));
        assert_eq!(
            serde_json::from_str::<Value>(&result.output).unwrap()["failure_code"],
            "bridge_protocol_failed"
        );
    }

    #[cfg(unix)]
    async fn supervise_script(script: &str) -> SupervisionResult {
        let (mut child, stdout) = shell_bridge(script);
        supervise_bridge(
            &mut child,
            stdout,
            Duration::from_secs(1),
            Duration::from_secs(1),
        )
        .await
    }

    #[cfg(unix)]
    fn terminal_json(success: bool, failure_code: &str) -> String {
        serde_json::json!({
            "schema_version": 1,
            "success": success,
            "last_completed_phase": if success { "cleanup_completed" } else { "arguments_validated" },
            "failure_code": failure_code,
            "packaged_runtime_identity": if failure_code == "packaged_runtime_identity_failed" { "failed" } else { "validated" },
            "selected_backend": "cpu",
            "warm_load_result": if success { "ready" } else { "not_started" },
            "authoritative_evidence_result": if success { "validated" } else { "failed" }
        })
        .to_string()
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn supervisor_preserves_complete_pre_start_failures() {
        for code in [
            "packaged_runtime_identity_failed",
            "unsupported_backend",
            "mock_runtime_rejected",
        ] {
            let record = terminal_json(false, code);
            let result = supervise_script(&format!("printf '%s\\n' '{record}'; exit 4")).await;
            assert_eq!(result.output, record);
            assert_eq!(
                serde_json::from_str::<Value>(&result.output).unwrap()["failure_code"],
                code
            );
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn supervisor_rejects_success_before_startup_and_incomplete_or_extra_results() {
        let success = terminal_json(true, "none");
        for record in [
            success,
            r#"{"schema_version":1,"success":false}"#.to_owned(),
            format!(
                "{}",
                terminal_json(false, "unsupported_backend").replace("}", ",\"extra\":true}")
            ),
        ] {
            let result = supervise_script(&format!("printf '%s\\n' '{record}'; exit 4")).await;
            assert_eq!(
                serde_json::from_str::<Value>(&result.output).unwrap()["failure_code"],
                "bridge_protocol_failed"
            );
            assert_ne!(result.output, record);
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn supervisor_requires_exit_status_to_match_and_preserves_post_start_results() {
        let ready = r#"{"type":"headless_internal","phase":"startup_ready"}"#;
        let success = terminal_json(true, "none");
        let failure = terminal_json(false, "warm_load_failed")
            .replace("arguments_validated", "runtime_identity_validated");
        for (record, exit, forwarded) in [
            (&success, 0, true),
            (&failure, 5, true),
            (&success, 5, false),
            (&failure, 0, false),
        ] {
            let result = supervise_script(&format!(
                "printf '%s\\n%s\\n' '{ready}' '{record}'; exit {exit}"
            ))
            .await;
            if forwarded {
                assert_eq!(result.output, *record);
            } else {
                assert_eq!(
                    serde_json::from_str::<Value>(&result.output).unwrap()["failure_code"],
                    "bridge_protocol_failed"
                );
            }
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn operation_timeout_reaps_root_and_descendant() {
        let directory = tempfile::tempdir().unwrap();
        let descendant_path = directory.path().join("descendant");
        let script = format!(
            "printf '{{\"type\":\"headless_internal\",\"phase\":\"startup_ready\"}}\\n'; \
             sleep 30 & printf '%s' \"$!\" > {}; wait",
            descendant_path.display()
        );
        let (mut child, stdout) = shell_bridge(&script);
        let root = child.id().unwrap();
        let result = supervise_bridge(
            &mut child,
            stdout,
            Duration::from_secs(1),
            Duration::from_millis(100),
        )
        .await;
        let value: Value = serde_json::from_str(&result.output).unwrap();
        assert_eq!(value["failure_code"], "operation_timeout");
        assert_eq!(value["packaged_runtime_identity"], "validated");
        assert_eq!(value["last_completed_phase"], "runtime_identity_validated");
        assert!(child.try_wait().unwrap().is_some(), "root was not reaped");
        assert!(compute_node::terminate_bridge_process_tree(root).await);
        let descendant: u32 = std::fs::read_to_string(descendant_path)
            .unwrap()
            .parse()
            .unwrap();
        let stat = std::fs::read_to_string(format!("/proc/{descendant}/stat"));
        assert!(
            stat.is_err()
                || stat
                    .unwrap()
                    .split(')')
                    .nth(1)
                    .is_some_and(|fields| fields.trim_start().starts_with('Z')),
            "descendant remained live"
        );
    }
}
