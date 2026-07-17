use std::path::{Path, PathBuf};
use std::process::Command;

use crate::backend::ComputeMode;

pub const ENABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP";
pub const DISABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP";
pub const BUNDLED_RUNTIME_RELATIVE_PYTHON: &str = if cfg!(target_os = "windows") {
    "python-runtime/python.exe"
} else {
    "python-runtime/bin/python3"
};
pub const DESKTOP_PYTHON_RUNTIME_MISSING: &str = "desktop_python_runtime_missing";
pub const DESKTOP_PYTHON_RUNTIME_INVALID: &str = "desktop_python_runtime_invalid";
pub const DESKTOP_PYTHON_OVERRIDE_INVALID: &str = "desktop_python_override_invalid";
pub const DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING: &str =
    "desktop_python_development_dependency_missing";

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PythonLauncherSource {
    EnvironmentOverride,
    BundledRuntime,
    SystemDevelopmentRuntime,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PythonLauncher {
    pub program: String,
    pub args: Vec<String>,
    pub source: PythonLauncherSource,
    pub runtime_id: String,
}

impl PythonLauncher {
    fn new(
        program: impl Into<String>,
        args: Vec<String>,
        source: PythonLauncherSource,
        runtime_id: impl Into<String>,
    ) -> Self {
        Self {
            program: program.into(),
            args,
            source,
            runtime_id: runtime_id.into(),
        }
    }

    fn command_for_version_check(&self) -> Command {
        let mut cmd = Command::new(&self.program);
        cmd.args(&self.args);
        cmd.arg("--version");
        cmd
    }

    fn command_for_metadata_probe(&self) -> Command {
        let mut cmd = Command::new(&self.program);
        cmd.args(&self.args);
        cmd.arg("-c");
        cmd.arg("import json,platform,sys; print(json.dumps({'version': list(sys.version_info[:2]), 'machine': platform.machine(), 'executable': sys.executable, 'prefix': sys.prefix}))");
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

#[allow(dead_code)]
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PythonLauncherCategory {
    BundledRuntimeMissing,
    BundledRuntimeNotExecutable,
    BundledRuntimeNotPython3,
    BundledRuntimeWrongArchitecture,
    BundledRuntimeProbeFailed,
    OverrideMissing,
    OverrideNotPython3,
    SystemRuntimeMissing,
    AppleDeveloperToolsStub,
    LauncherSpawnFailed,
}

impl PythonLauncherCategory {
    fn as_str(&self) -> &'static str {
        match self {
            Self::BundledRuntimeMissing => "bundled_runtime_missing",
            Self::BundledRuntimeNotExecutable => "bundled_runtime_not_executable",
            Self::BundledRuntimeNotPython3 => "bundled_runtime_not_python3",
            Self::BundledRuntimeWrongArchitecture => "bundled_runtime_wrong_architecture",
            Self::BundledRuntimeProbeFailed => "bundled_runtime_probe_failed",
            Self::OverrideMissing => "override_missing",
            Self::OverrideNotPython3 => "override_not_python3",
            Self::SystemRuntimeMissing => "system_runtime_missing",
            Self::AppleDeveloperToolsStub => "apple_developer_tools_stub",
            Self::LauncherSpawnFailed => "launcher_spawn_failed",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PythonLauncherError {
    pub public_code: &'static str,
    pub category: PythonLauncherCategory,
    pub source: PythonLauncherSource,
    pub executable_basename: String,
    pub exit_status: Option<i32>,
    pub packaged: bool,
}

impl std::fmt::Display for PythonLauncherError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} category={} source={:?} executable={} expected_python=3.11 expected_arch={} packaged={}{}",
            self.public_code, self.category.as_str(), self.source, self.executable_basename, expected_runtime_arch(), self.packaged,
            self.exit_status.map(|s| format!(" exit_status={s}")).unwrap_or_default())
    }
}
impl std::error::Error for PythonLauncherError {}

fn expected_runtime_arch() -> &'static str {
    if cfg!(target_os = "windows") {
        "AMD64"
    } else if cfg!(target_os = "macos") {
        "arm64"
    } else if cfg!(target_arch = "x86_64") {
        "x86_64"
    } else if cfg!(target_arch = "aarch64") {
        "arm64"
    } else {
        "unknown"
    }
}

fn bundled_runtime_id() -> &'static str {
    if cfg!(target_os = "windows") {
        "bundled-cpython-3.11-win-x86_64-cu124"
    } else if cfg!(target_os = "macos") {
        "bundled-cpython-3.11-macos-arm64"
    } else {
        "bundled-cpython-3.11-unknown"
    }
}

fn basename(path: &str) -> String {
    Path::new(path)
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or("python")
        .to_string()
}

fn default_python_candidates() -> Vec<PythonLauncher> {
    if cfg!(target_os = "windows") {
        return vec![
            PythonLauncher::new(
                "py",
                vec!["-3".into()],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "windows-py-launcher",
            ),
            PythonLauncher::new(
                "python",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "windows-python",
            ),
            PythonLauncher::new(
                "python3",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "windows-python3",
            ),
        ];
    }
    vec![
        PythonLauncher::new(
            "python3",
            vec![],
            PythonLauncherSource::SystemDevelopmentRuntime,
            "system-python3",
        ),
        PythonLauncher::new(
            "python",
            vec![],
            PythonLauncherSource::SystemDevelopmentRuntime,
            "system-python",
        ),
    ]
}

fn env_python_candidate(var_name: &str) -> Option<PythonLauncher> {
    std::env::var(var_name).ok().map(|value| {
        PythonLauncher::new(
            value,
            vec![],
            PythonLauncherSource::EnvironmentOverride,
            "env-override",
        )
    })
}

fn looks_like_windows_store_alias(stderr: &str) -> bool {
    stderr.to_ascii_lowercase().contains("python was not found")
}
fn looks_like_apple_developer_tools_stub(stdout: &str, stderr: &str) -> bool {
    let combined = format!("{stdout}\n{stderr}").to_ascii_lowercase();
    combined.contains("xcode-select")
        || combined.contains("no developer tools were found")
        || combined.contains("commandlinetools")
}
fn is_python_3_version(stdout: &str, stderr: &str) -> bool {
    format!("{stdout}\n{stderr}")
        .lines()
        .map(str::trim)
        .any(|l| l.starts_with("Python 3."))
}
fn is_python_311_version(stdout: &str, stderr: &str) -> bool {
    format!("{stdout}\n{stderr}")
        .lines()
        .map(str::trim)
        .any(|l| l.starts_with("Python 3.11."))
}
fn output_status_code(output: &std::process::Output) -> Option<i32> {
    output.status.code()
}

fn json_string_field<'a>(payload: &'a str, key: &str) -> Option<&'a str> {
    let needle = format!("\"{key}\"");
    let after_key = payload.split(&needle).nth(1)?;
    let after_colon = after_key.split_once(':')?.1.trim_start();
    let after_quote = after_colon.strip_prefix('"')?;
    after_quote.split('"').next()
}

fn metadata_probe_is_valid(
    stdout: &str,
    runtime_root: &Path,
    expected_machine: &str,
) -> Result<(), PythonLauncherCategory> {
    let payload = stdout.trim();
    if !(payload.contains("\"version\"") && payload.contains("[3, 11]")) {
        return Err(PythonLauncherCategory::BundledRuntimeNotPython3);
    }
    if json_string_field(payload, "machine") != Some(expected_machine) {
        return Err(PythonLauncherCategory::BundledRuntimeWrongArchitecture);
    }
    let runtime_root = runtime_root
        .canonicalize()
        .map_err(|_| PythonLauncherCategory::BundledRuntimeProbeFailed)?;
    for key in ["executable", "prefix"] {
        let value = json_string_field(payload, key)
            .ok_or(PythonLauncherCategory::BundledRuntimeProbeFailed)?;
        let resolved = Path::new(value)
            .canonicalize()
            .map_err(|_| PythonLauncherCategory::BundledRuntimeProbeFailed)?;
        if !resolved.starts_with(&runtime_root) {
            return Err(PythonLauncherCategory::BundledRuntimeProbeFailed);
        }
    }
    Ok(())
}

fn launcher_error(
    public_code: &'static str,
    category: PythonLauncherCategory,
    candidate: Option<&PythonLauncher>,
    packaged: bool,
    status: Option<i32>,
) -> PythonLauncherError {
    let source = candidate
        .map(|c| c.source.clone())
        .unwrap_or(PythonLauncherSource::SystemDevelopmentRuntime);
    let executable_basename = candidate
        .map(|c| basename(&c.program))
        .unwrap_or_else(|| "python".into());
    PythonLauncherError {
        public_code,
        category,
        source,
        executable_basename,
        exit_status: status,
        packaged,
    }
}

fn validate_launcher_with_output(
    candidate: &PythonLauncher,
    output: &std::process::Output,
    packaged: bool,
    require_311_arm64: bool,
) -> Result<(), PythonLauncherError> {
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if output.status.success()
        && !looks_like_windows_store_alias(&stderr)
        && is_python_3_version(&stdout, &stderr)
    {
        if require_311_arm64 && !is_python_311_version(&stdout, &stderr) {
            return Err(launcher_error(
                DESKTOP_PYTHON_RUNTIME_INVALID,
                PythonLauncherCategory::BundledRuntimeNotPython3,
                Some(candidate),
                packaged,
                output_status_code(output),
            ));
        }
        return Ok(());
    }
    if looks_like_apple_developer_tools_stub(&stdout, &stderr) {
        return Err(launcher_error(
            DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING,
            PythonLauncherCategory::AppleDeveloperToolsStub,
            Some(candidate),
            packaged,
            output_status_code(output),
        ));
    }
    let code = if candidate.source == PythonLauncherSource::EnvironmentOverride {
        DESKTOP_PYTHON_OVERRIDE_INVALID
    } else if candidate.source == PythonLauncherSource::BundledRuntime {
        DESKTOP_PYTHON_RUNTIME_INVALID
    } else {
        DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING
    };
    let category = if candidate.source == PythonLauncherSource::EnvironmentOverride {
        PythonLauncherCategory::OverrideNotPython3
    } else if candidate.source == PythonLauncherSource::BundledRuntime {
        PythonLauncherCategory::BundledRuntimeNotPython3
    } else {
        PythonLauncherCategory::SystemRuntimeMissing
    };
    Err(launcher_error(
        code,
        category,
        Some(candidate),
        packaged,
        output_status_code(output),
    ))
}

fn resolve_python_launcher_with_probe<F>(
    _var_name: &str,
    candidates: Vec<PythonLauncher>,
    mut probe: F,
) -> Result<PythonLauncher, PythonLauncherError>
where
    F: FnMut(&PythonLauncher) -> std::io::Result<std::process::Output>,
{
    let mut last_error: Option<PythonLauncherError> = None;
    for candidate in candidates {
        match probe(&candidate) {
            Ok(output) => match validate_launcher_with_output(&candidate, &output, false, false) {
                Ok(()) => return Ok(candidate),
                Err(err) => last_error = Some(err),
            },
            Err(_) => {
                let code = if candidate.source == PythonLauncherSource::EnvironmentOverride {
                    DESKTOP_PYTHON_OVERRIDE_INVALID
                } else if candidate.source == PythonLauncherSource::BundledRuntime {
                    DESKTOP_PYTHON_RUNTIME_INVALID
                } else {
                    DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING
                };
                last_error = Some(launcher_error(
                    code,
                    PythonLauncherCategory::LauncherSpawnFailed,
                    Some(&candidate),
                    false,
                    None,
                ))
            }
        }
    }
    Err(last_error.unwrap_or_else(|| {
        launcher_error(
            DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING,
            PythonLauncherCategory::SystemRuntimeMissing,
            None,
            false,
            None,
        )
    }))
}

pub fn resolve_python_launcher(var_name: &str) -> anyhow::Result<PythonLauncher> {
    let mut candidates = Vec::new();
    if let Some(env_candidate) = env_python_candidate(var_name) {
        candidates.push(env_candidate);
    }
    candidates.extend(default_python_candidates());
    resolve_python_launcher_with_probe(var_name, candidates, |candidate| {
        candidate.command_for_version_check().output()
    })
    .map_err(anyhow::Error::from)
}

#[derive(Debug, Clone)]
pub struct PythonLauncherResolutionOptions<'a> {
    pub override_var_name: &'a str,
    pub tauri_resource_dir: Option<&'a Path>,
    pub current_exe_path: Option<&'a Path>,
    pub manifest_dir: &'a Path,
    pub packaged: bool,
}

fn bundled_runtime_candidate(opts: &PythonLauncherResolutionOptions<'_>) -> Option<PythonLauncher> {
    let root = if let Some(resource_dir) = opts.tauri_resource_dir {
        Some(resource_dir.to_path_buf())
    } else {
        resource_root_candidates(opts.current_exe_path, opts.manifest_dir, None)
            .into_iter()
            .find(|c| {
                c.layout == ResourceLayoutKind::MacOsAppResources
                    || c.layout == ResourceLayoutKind::WindowsResources
                    || c.layout == ResourceLayoutKind::TauriResourceDir
                    || c.layout == ResourceLayoutKind::DevSourceTree
            })
            .map(|c| c.root)
    }?;
    Some(PythonLauncher::new(
        root.join(BUNDLED_RUNTIME_RELATIVE_PYTHON)
            .to_string_lossy()
            .to_string(),
        vec![],
        PythonLauncherSource::BundledRuntime,
        bundled_runtime_id(),
    ))
}

fn bundled_runtime_root_from_candidate(candidate: &PythonLauncher) -> Option<PathBuf> {
    Path::new(&candidate.program)
        .parent()
        .and_then(|parent| {
            if parent.file_name().and_then(|s| s.to_str()) == Some("bin") {
                parent.parent()
            } else {
                Some(parent)
            }
        })
        .map(Path::to_path_buf)
}

pub fn resolve_python_launcher_resource_aware(
    opts: PythonLauncherResolutionOptions<'_>,
) -> Result<PythonLauncher, PythonLauncherError> {
    if let Some(env_candidate) = env_python_candidate(opts.override_var_name) {
        return match env_candidate.command_for_version_check().output() {
            Ok(output) => {
                validate_launcher_with_output(&env_candidate, &output, opts.packaged, false)
                    .map(|_| env_candidate)
            }
            Err(_) => Err(launcher_error(
                DESKTOP_PYTHON_OVERRIDE_INVALID,
                PythonLauncherCategory::OverrideMissing,
                Some(&env_candidate),
                opts.packaged,
                None,
            )),
        };
    }
    let is_bundled_required_platform = cfg!(target_os = "macos") || cfg!(target_os = "windows");
    let is_macos = cfg!(target_os = "macos")
        || opts
            .current_exe_path
            .map(|p| p.components().any(|c| c.as_os_str() == "Contents"))
            .unwrap_or(false);
    if is_bundled_required_platform || is_macos {
        if let Some(candidate) = bundled_runtime_candidate(&opts) {
            if !Path::new(&candidate.program).exists() {
                if opts.packaged {
                    return Err(launcher_error(
                        DESKTOP_PYTHON_RUNTIME_MISSING,
                        PythonLauncherCategory::BundledRuntimeMissing,
                        Some(&candidate),
                        true,
                        None,
                    ));
                }
            } else {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    if std::fs::metadata(&candidate.program)
                        .map(|m| m.permissions().mode() & 0o111 == 0)
                        .unwrap_or(true)
                    {
                        return Err(launcher_error(
                            DESKTOP_PYTHON_RUNTIME_INVALID,
                            PythonLauncherCategory::BundledRuntimeNotExecutable,
                            Some(&candidate),
                            opts.packaged,
                            None,
                        ));
                    }
                }
                return match candidate.command_for_metadata_probe().output() {
                    Ok(output) => {
                        let stdout = String::from_utf8_lossy(&output.stdout);
                        let stderr = String::from_utf8_lossy(&output.stderr);
                        if looks_like_apple_developer_tools_stub(&stdout, &stderr) {
                            return Err(launcher_error(
                                DESKTOP_PYTHON_RUNTIME_INVALID,
                                PythonLauncherCategory::AppleDeveloperToolsStub,
                                Some(&candidate),
                                opts.packaged,
                                output_status_code(&output),
                            ));
                        }
                        let runtime_root = bundled_runtime_root_from_candidate(&candidate)
                            .ok_or_else(|| {
                                launcher_error(
                                    DESKTOP_PYTHON_RUNTIME_INVALID,
                                    PythonLauncherCategory::BundledRuntimeProbeFailed,
                                    Some(&candidate),
                                    opts.packaged,
                                    output_status_code(&output),
                                )
                            })?;
                        metadata_probe_is_valid(&stdout, &runtime_root, expected_runtime_arch())
                            .map(|_| candidate.clone())
                            .map_err(|category| {
                                launcher_error(
                                    DESKTOP_PYTHON_RUNTIME_INVALID,
                                    category,
                                    Some(&candidate),
                                    opts.packaged,
                                    output_status_code(&output),
                                )
                            })
                    }
                    Err(_) => Err(launcher_error(
                        if opts.packaged {
                            DESKTOP_PYTHON_RUNTIME_INVALID
                        } else {
                            DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING
                        },
                        PythonLauncherCategory::BundledRuntimeProbeFailed,
                        Some(&candidate),
                        opts.packaged,
                        None,
                    )),
                };
            }
        } else if opts.packaged {
            return Err(launcher_error(
                DESKTOP_PYTHON_RUNTIME_MISSING,
                PythonLauncherCategory::BundledRuntimeMissing,
                None,
                true,
                None,
            ));
        }
        if opts.packaged {
            return Err(launcher_error(
                DESKTOP_PYTHON_RUNTIME_MISSING,
                PythonLauncherCategory::BundledRuntimeMissing,
                None,
                true,
                None,
            ));
        }
    }
    resolve_python_launcher(opts.override_var_name).map_err(|_| {
        launcher_error(
            DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING,
            PythonLauncherCategory::SystemRuntimeMissing,
            None,
            false,
            None,
        )
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
            PythonLauncher::new(
                "py",
                vec!["-3".into()],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
            PythonLauncher::new(
                "python",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
            PythonLauncher::new(
                "python3",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
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
            PythonLauncher::new(
                "definitely-missing-python",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
            PythonLauncher::new(
                "python3",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
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
        assert!(message.contains(DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING));
        assert!(message.contains("category=system_runtime_missing"));
        assert!(!message.contains("not executable"));
        assert!(!message.contains("definitely-missing-python"));
    }

    #[test]
    fn windows_store_alias_message_falls_through_to_next_candidate() {
        let candidates = vec![
            PythonLauncher::new(
                "python",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
            PythonLauncher::new(
                "python3",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
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
    fn final_error_omits_raw_attempted_launcher_details() {
        let candidates = vec![
            PythonLauncher::new(
                "python",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
            PythonLauncher::new(
                "python3",
                vec![],
                PythonLauncherSource::SystemDevelopmentRuntime,
                "test",
            ),
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
        assert!(msg.contains(DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING));
        assert!(!msg.contains("Python 2.7.18"));
        assert!(!msg.contains("spawn failed: missing"));
    }

    #[test]
    fn spawn_failure_uses_candidate_source_specific_error_code() {
        let candidates = vec![PythonLauncher::new(
            "/missing/bundled/python3",
            vec![],
            PythonLauncherSource::BundledRuntime,
            "test",
        )];

        let err = resolve_python_launcher_with_probe("TOKEN_PLACE_TEST_PYTHON", candidates, |_| {
            Err(std::io::Error::new(std::io::ErrorKind::NotFound, "missing"))
        })
        .expect_err("expected bundled runtime spawn failure");

        assert_eq!(err.public_code, DESKTOP_PYTHON_RUNTIME_INVALID);
        assert_eq!(err.category, PythonLauncherCategory::LauncherSpawnFailed);
        assert_eq!(err.source, PythonLauncherSource::BundledRuntime);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn packaged_linux_retains_system_python_fallback() {
        let launcher = resolve_python_launcher_resource_aware(PythonLauncherResolutionOptions {
            override_var_name: "TOKEN_PLACE_TEST_PYTHON_NOT_SET",
            tauri_resource_dir: None,
            current_exe_path: None,
            manifest_dir: Path::new(env!("CARGO_MANIFEST_DIR")),
            packaged: true,
        })
        .expect("packaged Linux should probe installed system Python");

        assert_eq!(
            launcher.source,
            PythonLauncherSource::SystemDevelopmentRuntime
        );
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
    #[test]
    #[cfg(unix)]
    fn packaged_macos_selects_bundled_runtime_without_system_probe() {
        use std::os::unix::fs::PermissionsExt;
        let temp = TempDir::new().expect("tempdir");
        let resources = temp.path().join("App.app/Contents/Resources");
        let py = resources.join("python-runtime/bin/python3");
        std::fs::create_dir_all(py.parent().unwrap()).unwrap();
        let runtime_root = resources.join("python-runtime");
        let probe = format!(
            r#"{{"version":[3,11],"machine":"arm64","executable":"{}","prefix":"{}"}}"#,
            py.display(),
            runtime_root.display()
        );
        std::fs::write(&py, format!("#!/bin/sh\nprintf '%s\\n' '{}'\n", probe)).unwrap();
        let mut perms = std::fs::metadata(&py).unwrap().permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&py, perms).unwrap();
        let exe = temp
            .path()
            .join("App.app/Contents/MacOS/token.place desktop");
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        let launcher = resolve_python_launcher_resource_aware(PythonLauncherResolutionOptions {
            override_var_name: "TOKEN_PLACE_TEST_PYTHON_NOT_SET",
            tauri_resource_dir: None,
            current_exe_path: Some(&exe),
            manifest_dir: temp.path(),
            packaged: true,
        })
        .expect("bundled runtime");
        assert_eq!(launcher.source, PythonLauncherSource::BundledRuntime);
        assert_eq!(Path::new(&launcher.program), py.as_path());
    }

    #[test]
    fn apple_developer_tools_stub_is_sanitized() {
        let candidate = PythonLauncher::new(
            "python3",
            vec![],
            PythonLauncherSource::SystemDevelopmentRuntime,
            "test",
        );
        let output = fake_output(
            false,
            "",
            "xcode-select: note: No developer tools were found, requesting install",
        );
        let err = validate_launcher_with_output(&candidate, &output, false, false)
            .expect_err("stub rejected");
        let msg = err.to_string();
        assert_eq!(
            err.category,
            PythonLauncherCategory::AppleDeveloperToolsStub
        );
        assert!(!msg.contains("xcode-select"));
        assert!(!msg.contains("No developer tools"));
    }
}
