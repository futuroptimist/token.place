use std::process::Command;

#[derive(Debug, Clone)]
pub struct PythonLauncher {
    pub program: String,
    pub args: Vec<String>,
}

impl PythonLauncher {
    fn command_for_version_check(&self) -> Command {
        let mut cmd = Command::new(&self.program);
        cmd.args(&self.args);
        cmd.arg("--version");
        cmd
    }

    pub fn command_for_script(&self, script_path: &str) -> tokio::process::Command {
        let mut cmd = tokio::process::Command::new(&self.program);
        cmd.args(&self.args);
        cmd.arg(script_path);
        cmd
    }

    pub fn command_for_script_blocking(&self, script_path: &str) -> Command {
        let mut cmd = Command::new(&self.program);
        cmd.args(&self.args);
        cmd.arg(script_path);
        cmd
    }
}

fn default_python_candidates() -> Vec<PythonLauncher> {
    if cfg!(target_os = "windows") {
        return vec![
            PythonLauncher {
                program: "py".into(),
                args: vec!["-3".into()],
            },
            PythonLauncher {
                program: "python".into(),
                args: vec![],
            },
            PythonLauncher {
                program: "python3".into(),
                args: vec![],
            },
        ];
    }

    vec![
        PythonLauncher {
            program: "python3".into(),
            args: vec![],
        },
        PythonLauncher {
            program: "python".into(),
            args: vec![],
        },
    ]
}

fn env_python_candidate(var_name: &str) -> Option<PythonLauncher> {
    std::env::var(var_name).ok().map(|value| PythonLauncher {
        program: value,
        args: vec![],
    })
}

fn looks_like_windows_store_alias(stderr: &str) -> bool {
    stderr.to_ascii_lowercase().contains("python was not found")
}

fn is_python_3_version(stdout: &str, stderr: &str) -> bool {
    let combined = format!("{stdout}\n{stderr}");
    combined
        .lines()
        .map(str::trim)
        .any(|line| line.starts_with("Python 3."))
}

pub fn resolve_python_launcher(var_name: &str) -> anyhow::Result<PythonLauncher> {
    let mut candidates = Vec::new();
    if let Some(env_candidate) = env_python_candidate(var_name) {
        candidates.push(env_candidate);
    }
    candidates.extend(default_python_candidates());

    let mut attempts = Vec::new();

    for candidate in candidates {
        let output = candidate.command_for_version_check().output();
        match output {
            Ok(output) => {
                let stderr = String::from_utf8_lossy(&output.stderr).to_string();
                let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
                if output.status.success()
                    && !looks_like_windows_store_alias(&stderr)
                    && is_python_3_version(&stdout, &stderr)
                {
                    return Ok(candidate);
                }

                attempts.push(format!(
                    "{} {} -> status={} stdout='{}' stderr='{}'",
                    candidate.program,
                    candidate.args.join(" "),
                    output.status,
                    stdout,
                    stderr.trim()
                ));
            }
            Err(err) => {
                attempts.push(format!(
                    "{} {} -> spawn failed: {}",
                    candidate.program,
                    candidate.args.join(" "),
                    err
                ));
            }
        }
    }

    anyhow::bail!(
        "no usable Python 3 interpreter found for desktop Python subprocess (consulted override env var: {}); tried: {}",
        var_name,
        attempts.join("; ")
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_store_alias_detector_matches_expected_message() {
        assert!(looks_like_windows_store_alias(
            "Python was not found; run without arguments to install from the Microsoft Store"
        ));
        assert!(!looks_like_windows_store_alias("Python 3.12.0"));
    }

    #[test]
    #[cfg(target_os = "windows")]
    fn includes_windows_launcher_candidates() {
        let candidates = default_python_candidates();
        assert_eq!(candidates[0].program, "py");
        assert_eq!(candidates[0].args, vec!["-3".to_string()]);
    }

    #[test]
    fn requires_python_3_version() {
        assert!(is_python_3_version("Python 3.12.1", ""));
        assert!(is_python_3_version("", "Python 3.11.9"));
        assert!(!is_python_3_version("Python 2.7.18", ""));
    }
}
