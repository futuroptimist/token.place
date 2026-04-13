use std::path::{Path, PathBuf};
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

fn python_candidates(var_name: &str) -> Vec<PythonLauncher> {
    let mut candidates = Vec::new();
    if let Some(env_candidate) = env_python_candidate(var_name) {
        candidates.push(env_candidate);
    }
    candidates.extend(default_python_candidates());
    candidates
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

fn resolve_python_launcher_with_probe<F>(
    var_name: &str,
    candidates: Vec<PythonLauncher>,
    mut probe: F,
) -> anyhow::Result<PythonLauncher>
where
    F: FnMut(&PythonLauncher) -> std::io::Result<std::process::Output>,
{
    let mut attempts = Vec::new();

    for candidate in candidates {
        let probe_result = probe(&candidate);
        match probe_result {
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

pub fn resolve_python_launcher(var_name: &str) -> anyhow::Result<PythonLauncher> {
    let candidates = python_candidates(var_name);
    resolve_python_launcher_with_probe(var_name, candidates, |candidate| {
        candidate.command_for_version_check().output()
    })
}

pub fn resolve_runtime_import_root(
    script_path: Option<&Path>,
    manifest_dir: &Path,
) -> Option<PathBuf> {
    let mut candidates = Vec::new();

    if let Ok(explicit) = std::env::var("TOKEN_PLACE_PYTHON_IMPORT_ROOT") {
        let explicit = explicit.trim();
        if !explicit.is_empty() {
            candidates.push(PathBuf::from(explicit));
        }
    }

    if let Some(script_path) = script_path {
        if let Some(script_dir) = script_path.parent() {
            if let Some(script_root) = script_dir.parent() {
                candidates.push(script_root.to_path_buf());
                let mut up = script_root.to_path_buf();
                for _ in 0..2 {
                    up = up.join("_up_");
                    candidates.push(up.clone());
                }
                if let Some(root_parent) = script_root.parent() {
                    candidates.push(root_parent.to_path_buf());
                }
            }
        }
    }

    candidates.push(manifest_dir.to_path_buf());
    if let Some(parent) = manifest_dir.parent() {
        candidates.push(parent.to_path_buf());
        if let Some(grandparent) = parent.parent() {
            candidates.push(grandparent.to_path_buf());
        }
    }

    candidates.into_iter().find(|candidate| {
        candidate.join("utils").is_dir() || candidate.join("config.py").is_file()
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;
    #[cfg(unix)]
    use std::os::unix::process::ExitStatusExt;
    #[cfg(windows)]
    use std::os::windows::process::ExitStatusExt;
    use std::process::ExitStatus;

    fn fake_output(success: bool, stdout: &str, stderr: &str) -> std::process::Output {
        std::process::Output {
            status: if success {
                ExitStatus::from_raw(0)
            } else {
                ExitStatus::from_raw(1 << 8)
            },
            stdout: stdout.as_bytes().to_vec(),
            stderr: stderr.as_bytes().to_vec(),
        }
    }

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

    #[test]
    fn resolver_prefers_first_working_windows_candidate_order() {
        let candidates = vec![
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

        let mut probe_calls = Vec::new();
        let launcher =
            resolve_python_launcher_with_probe("TOKEN_PLACE_SIDECAR_PYTHON", candidates, |c| {
                probe_calls.push(
                    format!("{} {}", c.program, c.args.join(" "))
                        .trim()
                        .to_string(),
                );
                Ok(fake_output(true, "Python 3.12.2", ""))
            })
            .expect("resolve launcher");

        assert_eq!(launcher.program, "py");
        assert_eq!(launcher.args, vec!["-3".to_string()]);
        assert_eq!(probe_calls, vec!["py -3".to_string()]);
    }

    #[test]
    fn invalid_env_override_is_reported_when_all_candidates_fail() {
        let candidates = vec![
            PythonLauncher {
                program: "definitely-missing-python".into(),
                args: vec![],
            },
            PythonLauncher {
                program: "python3".into(),
                args: vec![],
            },
        ];

        let err =
            resolve_python_launcher_with_probe("TOKEN_PLACE_SIDECAR_PYTHON", candidates, |c| {
                if c.program == "definitely-missing-python" {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::NotFound,
                        "not found",
                    ));
                }
                Ok(fake_output(false, "", "not executable"))
            })
            .expect_err("expected failure");

        let message = err.to_string();
        assert!(message.contains("TOKEN_PLACE_SIDECAR_PYTHON"));
        assert!(message.contains("definitely-missing-python"));
        assert!(message.contains("spawn failed"));
    }

    #[test]
    fn windows_store_alias_message_falls_through_to_next_candidate() {
        let candidates = vec![
            PythonLauncher {
                program: "python".into(),
                args: vec![],
            },
            PythonLauncher {
                program: "python3".into(),
                args: vec![],
            },
        ];

        let launcher = resolve_python_launcher_with_probe("TOKEN_PLACE_SIDECAR_PYTHON", candidates, |c| {
            if c.program == "python" {
                return Ok(fake_output(
                    false,
                    "",
                    "Python was not found; run without arguments to install from the Microsoft Store",
                ));
            }
            Ok(fake_output(true, "Python 3.12.2", ""))
        })
        .expect("fallback to python3");

        assert_eq!(launcher.program, "python3");
    }

    #[test]
    fn final_error_contains_attempted_launcher_details() {
        let candidates = vec![
            PythonLauncher {
                program: "python".into(),
                args: vec![],
            },
            PythonLauncher {
                program: "python3".into(),
                args: vec![],
            },
        ];

        let err =
            resolve_python_launcher_with_probe("TOKEN_PLACE_SIDECAR_PYTHON", candidates, |c| {
                if c.program == "python" {
                    return Ok(fake_output(true, "Python 2.7.18", ""));
                }
                Err(std::io::Error::new(std::io::ErrorKind::NotFound, "missing"))
            })
            .expect_err("expected detailed failure");

        let msg = err.to_string();
        assert!(msg.contains("python  -> status="));
        assert!(msg.contains("Python 2.7.18"));
        assert!(msg.contains("python3  -> spawn failed"));
    }

    #[test]
    fn resolve_runtime_import_root_detects_nested_up_layout() {
        let temp = TempDir::new().expect("tempdir");
        let script = temp.path().join("resources").join("python").join("model_bridge.py");
        std::fs::create_dir_all(script.parent().expect("script parent")).expect("create script dir");
        std::fs::write(&script, "#!/usr/bin/env python3\n").expect("write script");
        let import_root = temp.path().join("resources").join("_up_").join("_up_");
        std::fs::create_dir_all(import_root.join("utils")).expect("create utils dir");

        let resolved = resolve_runtime_import_root(Some(&script), Path::new("/missing"));
        assert_eq!(resolved.as_deref(), Some(import_root.as_path()));
    }
}
