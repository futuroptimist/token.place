use std::path::{Path, PathBuf};
use std::process::Command;

use crate::backend::ComputeMode;

pub const ENABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP";
pub const DISABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP";

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

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResourceLayoutKind {
    TauriResourceDir,
    WindowsResources,
    LinuxResources,
    MacOsAppResources,
    ExecutablePythonSibling,
    DevSourceTree,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResourceRootCandidate {
    pub root: PathBuf,
    pub layout: ResourceLayoutKind,
}

fn push_unique_resource_root(
    candidates: &mut Vec<ResourceRootCandidate>,
    root: PathBuf,
    layout: ResourceLayoutKind,
) {
    if candidates.iter().any(|candidate| candidate.root == root) {
        return;
    }
    candidates.push(ResourceRootCandidate { root, layout });
}

fn exe_sibling_resources_layout() -> ResourceLayoutKind {
    if cfg!(target_os = "windows") {
        ResourceLayoutKind::WindowsResources
    } else {
        ResourceLayoutKind::LinuxResources
    }
}

pub fn resource_root_candidates(
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    tauri_resource_dir: Option<&Path>,
) -> Vec<ResourceRootCandidate> {
    let mut candidates = Vec::new();

    if let Some(resource_dir) = tauri_resource_dir {
        push_unique_resource_root(
            &mut candidates,
            resource_dir.to_path_buf(),
            ResourceLayoutKind::TauriResourceDir,
        );
    }

    if let Some(exe_path) = exe_path {
        if let Some(exe_dir) = exe_path.parent() {
            push_unique_resource_root(
                &mut candidates,
                exe_dir.join("resources"),
                exe_sibling_resources_layout(),
            );
            push_unique_resource_root(
                &mut candidates,
                exe_dir.join("python"),
                ResourceLayoutKind::ExecutablePythonSibling,
            );
            if let Some(contents_dir) = exe_dir.parent() {
                push_unique_resource_root(
                    &mut candidates,
                    contents_dir.join("Resources"),
                    ResourceLayoutKind::MacOsAppResources,
                );
                push_unique_resource_root(
                    &mut candidates,
                    contents_dir.join("resources"),
                    ResourceLayoutKind::LinuxResources,
                );
                push_unique_resource_root(
                    &mut candidates,
                    contents_dir.join("_up_").join("resources"),
                    ResourceLayoutKind::WindowsResources,
                );
            }
        }
    }

    push_unique_resource_root(
        &mut candidates,
        manifest_dir.to_path_buf(),
        ResourceLayoutKind::DevSourceTree,
    );
    candidates
}

pub fn bridge_script_candidates_from_resource_roots(
    script_name: &str,
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    tauri_resource_dir: Option<&Path>,
) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    for root_candidate in resource_root_candidates(exe_path, manifest_dir, tauri_resource_dir) {
        match root_candidate.layout {
            ResourceLayoutKind::ExecutablePythonSibling => {
                candidates.push(root_candidate.root.join(script_name));
            }
            ResourceLayoutKind::DevSourceTree => {
                candidates.push(root_candidate.root.join("python").join(script_name));
            }
            _ => {
                candidates.push(root_candidate.root.join("python").join(script_name));
                candidates.push(root_candidate.root.join(script_name));
            }
        }
    }
    candidates
}

pub fn describe_resource_layout(
    script_path: &Path,
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    tauri_resource_dir: Option<&Path>,
) -> (PathBuf, ResourceLayoutKind) {
    for candidate in resource_root_candidates(exe_path, manifest_dir, tauri_resource_dir) {
        if script_path.starts_with(&candidate.root) {
            return (candidate.root, candidate.layout);
        }
    }
    let root = script_path
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .unwrap_or_else(|| manifest_dir.to_path_buf());
    (root, ResourceLayoutKind::DevSourceTree)
}

pub fn disable_python_user_site<C>(command: &mut C)
where
    C: PythonEnvCommand,
{
    command.set_env("PYTHONNOUSERSITE", std::ffi::OsStr::new("1"));
}

pub fn configure_python_subprocess_env<C>(command: &mut C, import_root: &Path)
where
    C: PythonEnvCommand,
{
    disable_python_user_site(command);
    command.set_env("TOKEN_PLACE_PYTHON_IMPORT_ROOT", import_root.as_os_str());
    let python_dir = import_root.join("python");
    let pythonpath = if python_dir.is_dir() {
        std::env::join_paths([import_root, python_dir.as_path()])
            .unwrap_or_else(|_| import_root.as_os_str().to_owned())
    } else {
        import_root.as_os_str().to_owned()
    };
    command.set_env("PYTHONPATH", pythonpath);
}

pub trait PythonEnvCommand {
    fn set_env<K, V>(&mut self, key: K, value: V)
    where
        K: AsRef<std::ffi::OsStr>,
        V: AsRef<std::ffi::OsStr>;
}

impl PythonEnvCommand for Command {
    fn set_env<K, V>(&mut self, key: K, value: V)
    where
        K: AsRef<std::ffi::OsStr>,
        V: AsRef<std::ffi::OsStr>,
    {
        self.env(key, value);
    }
}

impl PythonEnvCommand for tokio::process::Command {
    fn set_env<K, V>(&mut self, key: K, value: V)
    where
        K: AsRef<std::ffi::OsStr>,
        V: AsRef<std::ffi::OsStr>,
    {
        self.env(key, value);
    }
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
    fn resource_root_candidates_support_macos_app_and_windows_resources() {
        let temp = TempDir::new().expect("tempdir");
        let mac_exe = temp
            .path()
            .join("TokenPlace.app")
            .join("Contents")
            .join("MacOS")
            .join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");

        let roots = resource_root_candidates(Some(&mac_exe), &manifest_dir, None);

        assert!(roots.iter().any(|candidate| {
            candidate.layout == ResourceLayoutKind::MacOsAppResources
                && candidate.root.ends_with("Contents/Resources")
        }));
        assert!(roots.iter().any(|candidate| {
            candidate.layout == ResourceLayoutKind::DevSourceTree && candidate.root == manifest_dir
        }));

        let exe = temp.path().join("App").join("token.place.exe");
        let exe_roots = resource_root_candidates(Some(&exe), &manifest_dir, None);
        let expected_layout = if cfg!(target_os = "windows") {
            ResourceLayoutKind::WindowsResources
        } else {
            ResourceLayoutKind::LinuxResources
        };
        assert!(exe_roots.iter().any(|candidate| {
            candidate.layout == expected_layout && candidate.root.ends_with("App/resources")
        }));
    }

    #[test]
    fn exe_sibling_resources_layout_matches_target_os() {
        let temp = TempDir::new().expect("tempdir");
        let exe = temp.path().join("bin").join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");

        let roots = resource_root_candidates(Some(&exe), &manifest_dir, None);
        let exe_resources = roots
            .iter()
            .find(|candidate| candidate.root.ends_with("bin/resources"))
            .expect("exe resources candidate");

        if cfg!(target_os = "windows") {
            assert_eq!(exe_resources.layout, ResourceLayoutKind::WindowsResources);
        } else {
            assert_eq!(exe_resources.layout, ResourceLayoutKind::LinuxResources);
        }
    }

    #[test]
    fn describe_resource_layout_reports_linux_exe_resources_on_non_windows() {
        if cfg!(target_os = "windows") {
            return;
        }
        let temp = TempDir::new().expect("tempdir");
        let exe = temp.path().join("bin").join("token.place");
        let script = temp
            .path()
            .join("bin")
            .join("resources")
            .join("python")
            .join("model_bridge.py");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");

        let (_root, layout) = describe_resource_layout(&script, Some(&exe), &manifest_dir, None);

        assert_eq!(layout, ResourceLayoutKind::LinuxResources);
    }

    #[test]
    fn bridge_script_candidates_are_generated_from_shared_resource_roots() {
        let temp = TempDir::new().expect("tempdir");
        let exe = temp
            .path()
            .join("TokenPlace.app")
            .join("Contents")
            .join("MacOS")
            .join("token.place");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");

        let model_candidates = bridge_script_candidates_from_resource_roots(
            "model_bridge.py",
            Some(&exe),
            &manifest_dir,
            None,
        );
        let compute_candidates = bridge_script_candidates_from_resource_roots(
            "compute_node_bridge.py",
            Some(&exe),
            &manifest_dir,
            None,
        );

        assert_eq!(model_candidates.len(), compute_candidates.len());
        assert!(model_candidates
            .iter()
            .any(|candidate| candidate.ends_with("Contents/Resources/python/model_bridge.py")));
        assert!(compute_candidates.iter().any(|candidate| {
            candidate.ends_with("Contents/Resources/python/compute_node_bridge.py")
        }));
    }

    #[test]
    fn configure_python_subprocess_env_uses_deterministic_pythonpath() {
        let temp = TempDir::new().expect("tempdir");
        let root = temp.path().join("Resources");
        std::fs::create_dir_all(root.join("python")).expect("create python dir");
        let mut command = Command::new("python");

        configure_python_subprocess_env(&mut command, &root);

        let envs: std::collections::HashMap<_, _> = command
            .get_envs()
            .filter_map(|(key, value)| {
                value.map(|value| {
                    (
                        key.to_string_lossy().into_owned(),
                        value.to_string_lossy().into_owned(),
                    )
                })
            })
            .collect();
        assert_eq!(envs.get("PYTHONNOUSERSITE").map(String::as_str), Some("1"));
        assert_eq!(
            envs.get("TOKEN_PLACE_PYTHON_IMPORT_ROOT")
                .map(String::as_str),
            Some(root.to_str().expect("root str"))
        );
        let pythonpath_entries: Vec<_> = std::env::split_paths(std::ffi::OsStr::new(
            envs.get("PYTHONPATH").expect("PYTHONPATH"),
        ))
        .collect();
        assert_eq!(pythonpath_entries, vec![root.clone(), root.join("python")]);
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
