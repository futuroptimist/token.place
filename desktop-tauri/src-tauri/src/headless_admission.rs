//! Installed-package, pre-Tauri CPU tokenizer/admission boundary.
use serde::Serialize;
use serde_json::Value;
use std::{
    ffi::OsString,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::Stdio,
    sync::mpsc,
    time::{Duration, Instant},
};
const FLAG: &str = "--headless-cpu-admission";
#[derive(Debug, Serialize)]
struct Record {
    schema_version: u8,
    success: bool,
    last_completed_phase: &'static str,
    failure_code: Option<&'static str>,
    packaged_runtime_identity: &'static str,
    selected_backend: &'static str,
    warm_load: &'static str,
    authoritative_evidence: &'static str,
}
impl Record {
    fn fail(phase: &'static str, code: &'static str, id: &'static str) -> Self {
        Self {
            schema_version: 1,
            success: false,
            last_completed_phase: phase,
            failure_code: Some(code),
            packaged_runtime_identity: id,
            selected_backend: "cpu",
            warm_load: "failed",
            authoritative_evidence: "failed",
        }
    }
}
pub fn is_requested<I: IntoIterator<Item = OsString>>(args: I) -> bool {
    args.into_iter().any(|a| a == FLAG)
}
fn value(args: &[OsString], name: &str) -> Result<String, &'static str> {
    let p = format!("{name}=");
    let v: Vec<_> = args
        .iter()
        .filter_map(|a| a.to_str())
        .filter_map(|a| a.strip_prefix(&p))
        .collect();
    if v.len() != 1 || v[0].is_empty() {
        Err("invalid_arguments")
    } else {
        Ok(v[0].into())
    }
}
fn parse(args: &[OsString]) -> Result<(PathBuf, String, Duration, Duration), &'static str> {
    let allowed = [
        FLAG,
        "--model=",
        "--backend=",
        "--context-tier=",
        "--startup-timeout-seconds=",
        "--operation-timeout-seconds=",
    ];
    if args.iter().any(|a| {
        a.to_str()
            .is_none_or(|s| !allowed.iter().any(|p| s == *p || s.starts_with(p)))
    }) {
        return Err("invalid_arguments");
    }
    if value(args, "--backend")? != "cpu" {
        return Err("unsupported_backend");
    };
    let tier = value(args, "--context-tier")?;
    if !matches!(
        tier.as_str(),
        "8k-fast" | "16k-balanced" | "32k-extended" | "64k-full"
    ) {
        return Err("unsupported_context_tier");
    }
    let seconds = |n| {
        value(args, n)?
            .parse::<u64>()
            .ok()
            .filter(|v| (1..=3600).contains(v))
            .ok_or("invalid_timeout")
    };
    Ok((
        PathBuf::from(value(args, "--model")?),
        tier,
        Duration::from_secs(seconds("--startup-timeout-seconds")?),
        Duration::from_secs(seconds("--operation-timeout-seconds")?),
    ))
}
fn emit(r: &Record) {
    println!("{}", serde_json::to_string(r).expect("record"))
}
pub fn run(args: Vec<OsString>) -> i32 {
    let (model, tier, startup, operation) = match parse(&args) {
        Ok(v) => v,
        Err(c) => {
            emit(&Record::fail("dispatch", c, "not_checked"));
            return 2;
        }
    };
    if !model.is_absolute() || !model.is_file() {
        emit(&Record::fail(
            "arguments_validated",
            "model_unavailable",
            "not_checked",
        ));
        return 2;
    }
    let exe = std::env::current_exe().ok();
    let context = crate::python_runtime::BridgeResourceContext {
        exe_path: exe.as_deref(),
        manifest_dir: Path::new(env!("CARGO_MANIFEST_DIR")),
        tauri_resource_dir: None,
    };
    if !context.packaged() {
        emit(&Record::fail(
            "arguments_validated",
            "installed_package_required",
            "failed",
        ));
        return 3;
    }
    let launcher = match crate::python_runtime::resolve_python_launcher_resource_aware(
        context.launcher_options("TOKEN_PLACE_PYTHON"),
    ) {
        Ok(v) if v.source == crate::python_runtime::PythonLauncherSource::BundledRuntime => v,
        _ => {
            emit(&Record::fail(
                "resource_layout_resolved",
                "packaged_runtime_identity_failed",
                "failed",
            ));
            return 3;
        }
    };
    let script = match context
        .resolve_bridge_script_path("headless_admission.py", Some(&launcher.program))
    {
        Ok(v) => v,
        Err(_) => {
            emit(&Record::fail(
                "packaged_runtime_validated",
                "bridge_missing",
                "passed",
            ));
            return 3;
        }
    };
    let mut command = match launcher.command_for_script_blocking(&script) {
        Ok(v) => v,
        Err(_) => {
            emit(&Record::fail(
                "resource_layout_resolved",
                "packaged_runtime_identity_failed",
                "failed",
            ));
            return 3;
        }
    };
    if let Some(root) =
        crate::python_runtime::resolve_runtime_import_root(Some(&script), context.manifest_dir)
    {
        let (_, layout) = context.describe_resource_layout(&script);
        crate::python_runtime::configure_python_subprocess_env_for_layout(
            &mut command,
            &root,
            layout,
            true,
        )
    }
    command
        .args(["--model", &model.to_string_lossy(), "--context-tier", &tier])
        .env(
            "TOKENPLACE_RUNTIME_ID",
            crate::build_identity::BUNDLED_RUNTIME_ID,
        )
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = match command.spawn() {
        Ok(v) => v,
        Err(_) => {
            emit(&Record::fail(
                "packaged_runtime_validated",
                "bridge_spawn_failed",
                "passed",
            ));
            return 4;
        }
    };
    let stdout = child.stdout.take().expect("piped");
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(BufReader::new(stdout).lines().next().transpose());
    });
    let began = Instant::now();
    let line = loop {
        if let Ok(v) = rx.try_recv() {
            break v;
        }
        if let Ok(Some(_)) = child.try_wait() {
            break Ok(None);
        }
        if began.elapsed() >= startup + operation {
            let _ = child.kill();
            let _ = child.wait();
            emit(&Record::fail(
                "packaged_runtime_validated",
                "operation_timeout",
                "passed",
            ));
            return 5;
        }
        std::thread::sleep(Duration::from_millis(25))
    };
    let _ = child.kill();
    let clean = child.wait().is_ok();
    let Ok(Some(line)) = line else {
        emit(&Record::fail(
            "packaged_runtime_validated",
            "bridge_exited_before_startup_event",
            "passed",
        ));
        return 4;
    };
    let Ok(p) = serde_json::from_str::<Value>(&line) else {
        emit(&Record::fail(
            "packaged_runtime_validated",
            "bridge_invalid_result",
            "passed",
        ));
        return 4;
    };
    if p["warm_load"] != "ready" {
        emit(&Record::fail(
            "packaged_runtime_validated",
            "warm_load_failed",
            "passed",
        ));
        return 6;
    }
    if p["authoritative_evidence"] != "passed" {
        let mut r = Record::fail("warm_load", "authoritative_evidence_failed", "passed");
        r.warm_load = "ready";
        emit(&r);
        return 7;
    }
    if !clean {
        emit(&Record::fail(
            "authoritative_evidence",
            "cleanup_failed",
            "passed",
        ));
        return 8;
    }
    emit(&Record {
        schema_version: 1,
        success: true,
        last_completed_phase: "cleanup",
        failure_code: None,
        packaged_runtime_identity: "passed",
        selected_backend: "cpu",
        warm_load: "ready",
        authoritative_evidence: "passed",
    });
    0
}
#[cfg(test)]
mod tests {
    use super::*;
    fn a(v: &[&str]) -> Vec<OsString> {
        v.iter().map(OsString::from).collect()
    }
    #[test]
    fn pre_tauri_dispatch_is_explicit() {
        assert!(is_requested(a(&[FLAG])));
        assert!(!is_requested(a(&["--build-identity-json"])))
    }
    #[test]
    fn validation_fails_closed() {
        assert_eq!(parse(&a(&[FLAG])).unwrap_err(), "invalid_arguments")
    }
    #[test]
    fn cpu_only() {
        let v = [
            FLAG,
            "--model=/m",
            "--backend=gpu",
            "--context-tier=8k-fast",
            "--startup-timeout-seconds=1",
            "--operation-timeout-seconds=1",
        ];
        assert_eq!(parse(&a(&v)).unwrap_err(), "unsupported_backend")
    }
    #[test]
    fn record_is_private() {
        let s = serde_json::to_string(&Record::fail(
            "warm_load",
            "authoritative_evidence_failed",
            "passed",
        ))
        .unwrap();
        assert!(!s.contains("path"));
        assert!(!s.contains("prompt"))
    }
}
