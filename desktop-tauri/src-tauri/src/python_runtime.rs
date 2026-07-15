use std::path::{Path, PathBuf};
use std::process::Command;

use crate::backend::ComputeMode;

pub const ENABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP";
pub const DISABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PythonLauncherSource {
    EnvironmentOverride,
    BundledRuntime,
    SystemDevelopmentRuntime,
}

#[derive(Debug, Clone)]
pub struct PythonLauncher {
    pub program: String,
    pub args: Vec<String>,
    pub source: PythonLauncherSource,
    pub runtime_id: String,
}

impl PythonLauncher {
    pub fn env_override(program: String) -> Self {
        Self {
            program,
            args: vec![],
            source: PythonLauncherSource::EnvironmentOverride,
            runtime_id: "override".into(),
        }
    }

    pub fn system(program: &str, args: Vec<String>) -> Self {
        Self {
            program: program.into(),
            args,
            source: PythonLauncherSource::SystemDevelopmentRuntime,
            runtime_id: program.into(),
        }
    }

    pub fn bundled(path: PathBuf) -> Self {
        Self {
            program: path.to_string_lossy().into_owned(),
            args: vec![],
            source: PythonLauncherSource::BundledRuntime,
            runtime_id: "bundled-python-3.11-arm64".into(),
        }
    }

    pub fn safe_program_basename(&self) -> String {
        Path::new(&self.program)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("python")
            .to_string()
    }
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
            PythonLauncher::system("py", vec!["-3".into()]),
            PythonLauncher::system("python", vec![]),
            PythonLauncher::system("python3", vec![]),
        ];
    }

    vec![
        PythonLauncher::system("python3", vec![]),
        PythonLauncher::system("python", vec![]),
    ]
}

fn env_python_candidate(var_name: &str) -> Option<PythonLauncher> {
    std::env::var(var_name)
        .ok()
        .map(|value| PythonLauncher::env_override(value))
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

fn looks_like_apple_developer_tools_stub(stdout: &str, stderr: &str) -> bool {
    let combined = format!("{stdout}\n{stderr}").to_ascii_lowercase();
    combined.contains("xcode-select")
        || combined.contains("no developer tools were found")
        || combined.contains("command line tools")
}

fn public_error_code(
    source: &PythonLauncherSource,
    category: &str,
    packaged: bool,
) -> &'static str {
    match (source, category, packaged) {
        (PythonLauncherSource::EnvironmentOverride, _, _) => "desktop_python_override_invalid",
        (PythonLauncherSource::BundledRuntime, "bundled_runtime_missing", _) => {
            "desktop_python_runtime_missing"
        }
        (PythonLauncherSource::BundledRuntime, _, _) => "desktop_python_runtime_invalid",
        (_, "system_runtime_missing", _) | (_, "apple_developer_tools_stub", _) => {
            "desktop_python_development_dependency_missing"
        }
        _ => "desktop_python_runtime_invalid",
    }
}

fn safe_launcher_error(
    code: &str,
    category: &str,
    candidate: Option<&PythonLauncher>,
    status: Option<i32>,
    packaged: bool,
) -> anyhow::Error {
    let (source, basename) = candidate
        .map(|c| (format!("{:?}", c.source), c.safe_program_basename()))
        .unwrap_or_else(|| ("SystemDevelopmentRuntime".into(), "python".into()));
    anyhow::anyhow!(
        "{code}: category={category}; source={source}; executable={basename}; status={}; expected_python=3.11; expected_architecture=arm64; mode={}",
        status.map(|s| s.to_string()).unwrap_or_else(|| "unavailable".into()),
        if packaged { "packaged" } else { "development" }
    )
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
    resolve_python_launcher_with_probe_mode(var_name, candidates, false, &mut probe)
}

fn resolve_python_launcher_with_probe_mode<F>(
    var_name: &str,
    candidates: Vec<PythonLauncher>,
    packaged: bool,
    probe: &mut F,
) -> anyhow::Result<PythonLauncher>
where
    F: FnMut(&PythonLauncher) -> std::io::Result<std::process::Output>,
{
    let mut last_category = "system_runtime_missing";
    let mut last_candidate: Option<PythonLauncher> = None;
    let mut last_status = None;

    for candidate in candidates {
        if matches!(candidate.source, PythonLauncherSource::BundledRuntime)
            && !Path::new(&candidate.program).is_file()
        {
            let code = public_error_code(&candidate.source, "bundled_runtime_missing", packaged);
            return Err(safe_launcher_error(
                code,
                "bundled_runtime_missing",
                Some(&candidate),
                None,
                packaged,
            ));
        }
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

                last_status = output.status.code();
                last_category = if looks_like_apple_developer_tools_stub(&stdout, &stderr) {
                    "apple_developer_tools_stub"
                } else if matches!(candidate.source, PythonLauncherSource::BundledRuntime) {
                    "bundled_runtime_not_python3"
                } else if matches!(candidate.source, PythonLauncherSource::EnvironmentOverride) {
                    "override_not_python3"
                } else {
                    "system_runtime_missing"
                };
                if matches!(candidate.source, PythonLauncherSource::EnvironmentOverride) {
                    let code = public_error_code(&candidate.source, last_category, packaged);
                    return Err(safe_launcher_error(
                        code,
                        last_category,
                        Some(&candidate),
                        last_status,
                        packaged,
                    ));
                }
                last_candidate = Some(candidate);
            }
            Err(_) => {
                last_category = if matches!(candidate.source, PythonLauncherSource::BundledRuntime)
                {
                    "bundled_runtime_not_executable"
                } else if matches!(candidate.source, PythonLauncherSource::EnvironmentOverride) {
                    "override_missing"
                } else {
                    "launcher_spawn_failed"
                };
                if matches!(
                    candidate.source,
                    PythonLauncherSource::EnvironmentOverride
                        | PythonLauncherSource::BundledRuntime
                ) {
                    let code = public_error_code(&candidate.source, last_category, packaged);
                    return Err(safe_launcher_error(
                        code,
                        last_category,
                        Some(&candidate),
                        None,
                        packaged,
                    ));
                }
                last_candidate = Some(candidate);
            }
        }
    }

    let code = if var_name.contains("PYTHON") {
        "desktop_python_development_dependency_missing"
    } else {
        "desktop_python_runtime_invalid"
    };
    Err(safe_launcher_error(
        code,
        last_category,
        last_candidate.as_ref(),
        last_status,
        packaged,
    ))
}

#[derive(Debug, Clone)]
pub struct PythonLauncherResolveOptions<'a> {
    pub override_env_var: &'a str,
    pub tauri_resource_dir: Option<&'a Path>,
    pub current_exe: Option<&'a Path>,
    pub packaged: bool,
}

fn bundled_runtime_path(
    resource_dir: Option<&Path>,
    current_exe: Option<&Path>,
) -> Option<PathBuf> {
    if let Some(resource_dir) = resource_dir {
        return Some(
            resource_dir
                .join("python-runtime")
                .join("bin")
                .join("python3"),
        );
    }
    current_exe
        .and_then(Path::parent)
        .and_then(Path::parent)
        .map(|contents| {
            contents
                .join("Resources")
                .join("python-runtime")
                .join("bin")
                .join("python3")
        })
}

pub fn resolve_python_launcher_with_options(
    options: PythonLauncherResolveOptions<'_>,
) -> anyhow::Result<PythonLauncher> {
    if let Some(env_candidate) = env_python_candidate(options.override_env_var) {
        return resolve_python_launcher_with_probe_mode(
            options.override_env_var,
            vec![env_candidate],
            options.packaged,
            &mut |candidate: &PythonLauncher| candidate.command_for_version_check().output(),
        );
    }

    if cfg!(target_os = "macos") || options.packaged {
        if let Some(path) = bundled_runtime_path(options.tauri_resource_dir, options.current_exe) {
            let bundled = PythonLauncher::bundled(path);
            if options.packaged || Path::new(&bundled.program).is_file() {
                return resolve_python_launcher_with_probe_mode(
                    options.override_env_var,
                    vec![bundled],
                    options.packaged,
                    &mut |candidate: &PythonLauncher| {
                        candidate.command_for_version_check().output()
                    },
                );
            }
        }
        if options.packaged {
            return Err(safe_launcher_error(
                "desktop_python_runtime_missing",
                "bundled_runtime_missing",
                None,
                None,
                true,
            ));
        }
    }

    resolve_python_launcher(options.override_env_var)
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
    bridge_script_candidates_from_candidates(
        script_name,
        &resource_root_candidates(exe_path, manifest_dir, tauri_resource_dir),
    )
}

fn bridge_script_candidates_from_candidates(
    script_name: &str,
    root_candidates: &[ResourceRootCandidate],
) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    for root_candidate in root_candidates {
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

pub fn resolve_bridge_script_path(
    script_name: &str,
    exe_path: Option<&Path>,
    manifest_dir: &Path,
    tauri_resource_dir: Option<&Path>,
    interpreter: Option<&str>,
) -> Result<PathBuf, String> {
    let root_candidates = resource_root_candidates(exe_path, manifest_dir, tauri_resource_dir);
    let bridge_candidates = bridge_script_candidates_from_candidates(script_name, &root_candidates);
    bridge_candidates
        .iter()
        .find(|candidate| candidate.is_file())
        .cloned()
        .ok_or_else(|| {
            format_bridge_script_resolution_error(
                script_name,
                &root_candidates,
                &bridge_candidates,
                interpreter,
            )
        })
}

pub fn format_bridge_script_resolution_error(
    script_name: &str,
    root_candidates: &[ResourceRootCandidate],
    bridge_candidates: &[PathBuf],
    interpreter: Option<&str>,
) -> String {
    let attempted_roots = if root_candidates.is_empty() {
        "<none>".into()
    } else {
        root_candidates
            .iter()
            .map(|candidate| format!("{:?}:{}", candidate.layout, candidate.root.display()))
            .collect::<Vec<_>>()
            .join(", ")
    };
    let attempted_bridge_paths = if bridge_candidates.is_empty() {
        "<none>".into()
    } else {
        bridge_candidates
            .iter()
            .map(|candidate| candidate.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    };
    let selected_layout = root_candidates
        .first()
        .map(|candidate| format!("{:?}", candidate.layout))
        .unwrap_or_else(|| "<unknown>".into());
    let interpreter = interpreter.unwrap_or("<unresolved>");
    format!(
        "unable to locate desktop Python bridge script '{script_name}'; selected_layout={selected_layout}; interpreter={interpreter}; attempted_resource_roots=[{attempted_roots}]; attempted_bridge_paths=[{attempted_bridge_paths}]"
    )
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
    if bootstrap_disabled || !mode_requests_gpu(mode) {
        return false;
    }

    let normalized_arch = if target_arch == "amd64" {
        "x86_64"
    } else {
        target_arch
    };

    (target_os == "windows" && normalized_arch == "x86_64")
        || (target_os == "macos" && matches!(normalized_arch, "aarch64" | "arm64" | "x86_64"))
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
            PythonLauncher::system("py", vec!["-3".into()]),
            PythonLauncher::system("python", vec![]),
            PythonLauncher::system("python3", vec![]),
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
            PythonLauncher::env_override("/Users/alice/bin/definitely-missing-python".into()),
            PythonLauncher::system("python3", vec![]),
        ];

        let err =
            resolve_python_launcher_with_probe("TOKEN_PLACE_SIDECAR_PYTHON", candidates, |c| {
                if c.program.ends_with("definitely-missing-python") {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::NotFound,
                        "not found",
                    ));
                }
                Ok(fake_output(false, "", "not executable"))
            })
            .expect_err("expected failure");

        let message = err.to_string();
        assert!(message.contains("desktop_python_override_invalid"));
        assert!(message.contains("executable=definitely-missing-python"));
        assert!(!message.contains("/Users/alice"));
    }

    #[test]
    fn windows_store_alias_message_falls_through_to_next_candidate() {
        let candidates = vec![
            PythonLauncher::system("python", vec![]),
            PythonLauncher::system("python3", vec![]),
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
            PythonLauncher::system("python", vec![]),
            PythonLauncher::system("python3", vec![]),
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
        assert!(msg.contains("desktop_python_development_dependency_missing"));
        assert!(!msg.contains("Python 2.7.18"));
        assert!(!msg.contains("spawn failed: missing"));
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
    fn runtime_bootstrap_enabled_for_supported_gpu_platforms() {
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
        assert!(should_enable_runtime_bootstrap_for(
            "macos",
            "aarch64",
            &ComputeMode::Auto,
            false
        ));
        assert!(should_enable_runtime_bootstrap_for(
            "macos",
            "arm64",
            &ComputeMode::Hybrid,
            false
        ));
        assert!(should_enable_runtime_bootstrap_for(
            "macos",
            "x86_64",
            &ComputeMode::Gpu,
            false
        ));
        assert!(!should_enable_runtime_bootstrap_for(
            "macos",
            "arm64",
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
