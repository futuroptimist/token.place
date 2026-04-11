use anyhow::Context;
use std::process::{Command as StdCommand, Stdio};
use tokio::process::Command;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PythonCommandSpec {
    pub program: String,
    pub args: Vec<String>,
}

fn python_candidates(env_override: Option<String>, os: &str) -> Vec<PythonCommandSpec> {
    let mut candidates = Vec::new();

    if let Some(program) = env_override {
        let trimmed = program.trim();
        if !trimmed.is_empty() {
            candidates.push(PythonCommandSpec {
                program: trimmed.to_string(),
                args: vec![],
            });
        }
    }

    if os == "windows" {
        candidates.push(PythonCommandSpec {
            program: "py".into(),
            args: vec!["-3".into()],
        });
        candidates.push(PythonCommandSpec {
            program: "python".into(),
            args: vec![],
        });
        candidates.push(PythonCommandSpec {
            program: "python3".into(),
            args: vec![],
        });
    } else {
        candidates.push(PythonCommandSpec {
            program: "python3".into(),
            args: vec![],
        });
        candidates.push(PythonCommandSpec {
            program: "python".into(),
            args: vec![],
        });
    }

    candidates
}

fn candidate_is_available(spec: &PythonCommandSpec) -> bool {
    let mut cmd = StdCommand::new(&spec.program);
    cmd.args(&spec.args)
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    cmd.status().is_ok_and(|status| status.success())
}

pub fn resolve_python_command(env_var_name: &str) -> anyhow::Result<PythonCommandSpec> {
    let override_value = std::env::var(env_var_name).ok();
    let candidates = python_candidates(override_value, std::env::consts::OS);
    for candidate in candidates {
        if candidate_is_available(&candidate) {
            return Ok(candidate);
        }
    }

    anyhow::bail!(
        "unable to find a working Python 3 runtime; install Python 3 or set {env_var_name} to \
         a valid interpreter path"
    )
}

pub fn command_for_python_script(script_path: &str, env_var_name: &str) -> anyhow::Result<Command> {
    let spec = resolve_python_command(env_var_name)
        .with_context(|| format!("failed to resolve Python runtime for script {script_path}"))?;
    let mut cmd = Command::new(spec.program);
    cmd.args(spec.args).arg(script_path);
    Ok(cmd)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_candidates_prioritize_py_launcher_then_python_binaries() {
        let candidates = python_candidates(None, "windows");
        assert_eq!(
            candidates,
            vec![
                PythonCommandSpec {
                    program: "py".into(),
                    args: vec!["-3".into()]
                },
                PythonCommandSpec {
                    program: "python".into(),
                    args: vec![]
                },
                PythonCommandSpec {
                    program: "python3".into(),
                    args: vec![]
                }
            ]
        );
    }

    #[test]
    fn env_override_is_first_candidate() {
        let candidates = python_candidates(Some("C:/Python312/python.exe".into()), "windows");
        assert_eq!(
            candidates.first(),
            Some(&PythonCommandSpec {
                program: "C:/Python312/python.exe".into(),
                args: vec![]
            })
        );
    }
}
