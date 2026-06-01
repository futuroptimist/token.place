use std::path::{Path, PathBuf};
use std::process::Command;

use crate::backend::ComputeMode;

pub const ENABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP";
pub const DISABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PythonResourceRoot {
    pub root: PathBuf,
    pub layout: &'static str,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedPythonResource {
    pub resource_root: PathBuf,
    pub resource_layout: &'static str,
    pub script_path: PathBuf,
}

fn push_resource_root(
    candidates: &mut Vec<PythonResourceRoot>,
    root: PathBuf,
    layout: &'static str,
) {
    if candidates.iter().any(|candidate| candidate.root == root) {
        return;
    }
    candidates.push(PythonResourceRoot { root, layout });
}

pub fn python_resource_root_candidates(
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    resource_dir: Option<&Path>,
) -> Vec<PythonResourceRoot> {
    let mut candidates = Vec::new();

    if let Some(resource_dir) = resource_dir {
        push_resource_root(
            &mut candidates,
            resource_dir.to_path_buf(),
            "tauri-resource-dir",
        );
    }

    if let Some(exe_path) = exe_path {
        if let Some(exe_dir) = exe_path.parent() {
            push_resource_root(
                &mut candidates,
                exe_dir.join("resources"),
                "executable-resources",
            );
            push_resource_root(
                &mut candidates,
                exe_dir.to_path_buf(),
                "executable-python-dir",
            );
            if let Some(parent_dir) = exe_dir.parent() {
                push_resource_root(
                    &mut candidates,
                    parent_dir.join("_up_").join("resources"),
                    "windows-updater-resources",
                );
                push_resource_root(
                    &mut candidates,
                    parent_dir.join("Resources"),
                    "macos-app-resources",
                );
                push_resource_root(
                    &mut candidates,
                    parent_dir.join("resources"),
                    "linux-app-resources",
                );
            }
        }
    }

    push_resource_root(&mut candidates, manifest_dir.to_path_buf(), "dev-src-tauri");
    candidates
}

pub fn python_resource_script_candidates(
    script_name: &str,
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    resource_dir: Option<&Path>,
) -> Vec<ResolvedPythonResource> {
    python_resource_root_candidates(exe_path, manifest_dir, resource_dir)
        .into_iter()
        .map(|candidate| ResolvedPythonResource {
            script_path: candidate.root.join("python").join(script_name),
            resource_root: candidate.root,
            resource_layout: candidate.layout,
        })
        .collect()
}

pub fn resolve_python_resource_script(
    script_name: &str,
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    resource_dir: Option<&Path>,
) -> anyhow::Result<ResolvedPythonResource> {
    let candidates =
        python_resource_script_candidates(script_name, exe_path, manifest_dir, resource_dir);
    candidates
        .iter()
        .find(|candidate| candidate.script_path.is_file())
        .cloned()
        .ok_or_else(|| {
            let attempted = candidates
                .iter()
                .map(|candidate| {
                    format!(
                        "{}:{}",
                        candidate.resource_layout,
                        candidate.script_path.to_string_lossy()
                    )
                })
                .collect::<Vec<_>>()
                .join("; ");
            anyhow::anyhow!(
                "unable to locate bundled Python resource script '{script_name}'; attempted resource roots/scripts: {attempted}"
            )
        })
}

pub fn configure_python_subprocess_env_blocking(
    command: &mut Command,
    script_path: &Path,
    manifest_dir: &Path,
) -> Option<PathBuf> {
    command.env("PYTHONNOUSERSITE", "1");
    let import_root = resolve_runtime_import_root(Some(script_path), manifest_dir);
    if let Some(import_root) = &import_root {
        command.env("TOKEN_PLACE_PYTHON_IMPORT_ROOT", import_root);
        let mut components = Vec::new();
        if let Some(script_dir) = script_path.parent() {
            components.push(script_dir.to_path_buf());
        }
        components.push(import_root.clone());
        if let Ok(joined) = std::env::join_paths(components) {
            command.env("PYTHONPATH", joined);
        } else {
            command.env("PYTHONPATH", import_root);
        }
    }
    import_root
}

pub fn configure_python_subprocess_env_async(
    command: &mut tokio::process::Command,
    script_path: &Path,
    manifest_dir: &Path,
) -> Option<PathBuf> {
    command.env("PYTHONNOUSERSITE", "1");
    let import_root = resolve_runtime_import_root(Some(script_path), manifest_dir);
    if let Some(import_root) = &import_root {
        command.env("TOKEN_PLACE_PYTHON_IMPORT_ROOT", import_root);
        let mut components = Vec::new();
        if let Some(script_dir) = script_path.parent() {
            components.push(script_dir.to_path_buf());
        }
        components.push(import_root.clone());
        if let Ok(joined) = std::env::join_paths(components) {
            command.env("PYTHONPATH", joined);
        } else {
            command.env("PYTHONPATH", import_root);
        }
    }
    import_root
}

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

    pub fn display(&self) -> String {
        format!("{} {}", self.program, self.args.join(" "))
            .trim()
            .to_string()
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

    candidates
        .into_iter()
        .find(|candidate| candidate.join("utils").is_dir() || candidate.join("config.py").is_file())
}

fn mode_requests_gpu(mode: &ComputeMode) -> bool {
    matches!(
        mode,
        ComputeMode::Auto | ComputeMode::Gpu | ComputeMode::Hybrid
    )
}

fn should_enable_runtime_bootstrap_for(
    target_os: &str,
    target_arch: &str,
    mode: &ComputeMode,
    bootstrap_disabled: bool,
) -> bool {
    target_os == "windows"
        && target_arch == "x86_64"
        && mode_requests_gpu(mode)
        && !bootstrap_disabled
}

pub fn should_enable_runtime_bootstrap(mode: &ComputeMode) -> bool {
    let bootstrap_disabled = std::env::var(DISABLE_RUNTIME_BOOTSTRAP_ENV)
        .map(|value| value.trim() == "1")
        .unwrap_or(false);

    should_enable_runtime_bootstrap_for(
        std::env::consts::OS,
        std::env::consts::ARCH,
        mode,
        bootstrap_disabled,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    use std::os::unix::process::ExitStatusExt;
    #[cfg(windows)]
    use std::os::windows::process::ExitStatusExt;
    use std::process::ExitStatus;
    use tempfile::TempDir;

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
        let script = temp
            .path()
            .join("resources")
            .join("python")
            .join("model_bridge.py");
        std::fs::create_dir_all(script.parent().expect("script parent"))
            .expect("create script dir");
        std::fs::write(&script, "#!/usr/bin/env python3\n").expect("write script");
        let import_root = temp.path().join("resources").join("_up_").join("_up_");
        std::fs::create_dir_all(import_root.join("utils")).expect("create utils dir");

        let resolved = resolve_runtime_import_root(Some(&script), Path::new("/missing"));
        assert_eq!(resolved.as_deref(), Some(import_root.as_path()));
    }

    #[test]
    fn shared_resource_helper_resolves_macos_app_resources() {
        let temp = TempDir::new().expect("tempdir");
        let resources = temp
            .path()
            .join("Token Place.app")
            .join("Contents")
            .join("Resources");
        let python_dir = resources.join("python");
        std::fs::create_dir_all(&python_dir).expect("create python dir");
        std::fs::write(python_dir.join("compute_node_bridge.py"), "print('ok')\n")
            .expect("write bridge");
        std::fs::create_dir_all(resources.join("utils")).expect("create utils");
        let exe_path = temp
            .path()
            .join("Token Place.app")
            .join("Contents")
            .join("MacOS")
            .join("token.place");

        let resolved = resolve_python_resource_script(
            "compute_node_bridge.py",
            Some(&exe_path),
            Path::new("/missing/src-tauri"),
            None,
        )
        .expect("resolve macOS resource bridge");

        assert_eq!(resolved.resource_root, resources);
        assert_eq!(resolved.resource_layout, "macos-app-resources");
    }

    #[test]
    fn subprocess_env_ignores_existing_pythonpath_and_disables_user_site() {
        let temp = TempDir::new().expect("tempdir");
        let root = temp.path().join("resources");
        let script = root.join("python").join("model_bridge.py");
        std::fs::create_dir_all(script.parent().expect("script parent"))
            .expect("create python dir");
        std::fs::create_dir_all(root.join("utils")).expect("create utils");
        let mut command = Command::new("python3");

        let import_root =
            configure_python_subprocess_env_blocking(&mut command, &script, Path::new("/missing"));
        assert_eq!(import_root.as_deref(), Some(root.as_path()));
        let env_value = |key: &str| {
            command
                .get_envs()
                .find_map(|(env_key, value)| (env_key == key).then_some(value))
                .flatten()
                .map(|value| value.to_string_lossy().into_owned())
        };
        assert_eq!(env_value("PYTHONNOUSERSITE").as_deref(), Some("1"));
        let pythonpath = env_value("PYTHONPATH").expect("PYTHONPATH set");
        assert!(pythonpath.contains(
            &script
                .parent()
                .expect("script parent")
                .to_string_lossy()
                .to_string()
        ));
        assert!(pythonpath.contains(&root.to_string_lossy().to_string()));
        assert!(!pythonpath.contains("should/not/leak"));
    }

    #[test]
    fn runtime_bootstrap_only_enabled_for_windows_x64_gpu_modes() {
        assert!(should_enable_runtime_bootstrap_for(
            "windows",
            "x86_64",
            &ComputeMode::Auto,
            false
        ));
        assert!(should_enable_runtime_bootstrap_for(
            "windows",
            "x86_64",
            &ComputeMode::Gpu,
            false
        ));
        assert!(should_enable_runtime_bootstrap_for(
            "windows",
            "x86_64",
            &ComputeMode::Hybrid,
            false
        ));
        assert!(!should_enable_runtime_bootstrap_for(
            "windows",
            "x86_64",
            &ComputeMode::Cpu,
            false
        ));
        assert!(!should_enable_runtime_bootstrap_for(
            "linux",
            "x86_64",
            &ComputeMode::Gpu,
            false
        ));
        assert!(!should_enable_runtime_bootstrap_for(
            "windows",
            "x86_64",
            &ComputeMode::Gpu,
            true
        ));
    }
}
