use std::path::{Path, PathBuf};
use std::process::Command;

use sha2::{Digest, Sha256};

use crate::backend::ComputeMode;

pub const ENABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP";
pub const DISABLE_RUNTIME_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP";
#[cfg(test)]
pub static RUNTIME_BOOTSTRAP_ENV_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
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
        cmd.arg("import json,platform,sys; print(json.dumps({'version': list(sys.version_info[:3]), 'machine': platform.machine(), 'executable': sys.executable, 'prefix': sys.prefix}))");
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
    crate::build_identity::BUNDLED_RUNTIME_ID
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
    let compact_payload = payload.replace(char::is_whitespace, "");
    if !(compact_payload.contains("\"version\"") && compact_payload.contains("[3,11,13]")) {
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

fn bundled_windows_manifest() -> Option<serde_json::Value> {
    serde_json::from_str(include_str!(
        "../python/embedded_python_runtime_windows_x86_64_manifest.json"
    ))
    .ok()
}

// Rust validates bundled Windows provenance before launching the interpreter,
// closing the pre-launch trust boundary without relying on mutable mtimes/sizes.
fn bundled_windows_provenance_is_valid(runtime_root: &Path) -> bool {
    let path = runtime_root.join("embedded_python_runtime_provenance.json");
    let Ok(text) = std::fs::read_to_string(path) else {
        return false;
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) else {
        return false;
    };
    let Some(manifest) = bundled_windows_manifest() else {
        return false;
    };
    let Some(required_dlls) = manifest
        .get("required_native_dlls")
        .and_then(|v| v.as_array())
    else {
        return false;
    };
    let Some(closure) = value.get("pe_dll_closure").and_then(|v| v.as_array()) else {
        return false;
    };
    if closure.is_empty() {
        return false;
    }
    let required_names: std::collections::BTreeSet<String> = required_dlls
        .iter()
        .filter_map(|v| v.as_str().map(|s| s.to_ascii_lowercase()))
        .collect();
    let mut closure_names = std::collections::BTreeSet::new();
    let mut closure_paths = std::collections::BTreeSet::new();
    for entry in closure {
        let Some(name) = entry.get("name").and_then(|v| v.as_str()) else {
            return false;
        };
        let Some(rel) = entry
            .get("path")
            .and_then(|v| v.as_str())
            .or_else(|| entry.get("name").and_then(|v| v.as_str()))
        else {
            return false;
        };
        if rel.is_empty()
            || Path::new(rel).is_absolute()
            || rel.split(&['/', '\\']).any(|p| p == "..")
        {
            return false;
        }
        let rel_key = rel.replace('\\', "/").to_ascii_lowercase();
        if !closure_paths.insert(rel_key) {
            return false;
        }
        let Some(expected_sha) = entry.get("sha256").and_then(|v| v.as_str()) else {
            return false;
        };
        let file_path = runtime_root.join(rel);
        let Ok(resolved_root) = runtime_root.canonicalize() else {
            return false;
        };
        let Ok(resolved_file) = file_path.canonicalize() else {
            return false;
        };
        if !resolved_file.starts_with(&resolved_root) || !resolved_file.is_file() {
            return false;
        }
        let Ok(bytes) = std::fs::read(&resolved_file) else {
            return false;
        };
        let actual_sha = format!("{:x}", Sha256::digest(&bytes));
        if actual_sha != expected_sha {
            return false;
        }
        if entry.get("machine").and_then(|v| v.as_str()) != Some("IMAGE_FILE_MACHINE_AMD64")
            || !entry.get("imports").is_some_and(|v| v.is_array())
        {
            return false;
        }
        closure_names.insert(name.to_ascii_lowercase());
    }
    value.get("runtime_id").and_then(|v| v.as_str())
        == Some("bundled-cpython-3.11-win-x86_64-cu124")
        && value.get("cpython_version") == manifest.get("cpython_version")
        && value.get("cpython_version").and_then(|v| v.as_str()) == Some("3.11.13")
        && value.get("target_triple") == manifest.get("target_triple")
        && value.get("target_triple").and_then(|v| v.as_str()) == Some("x86_64-pc-windows-msvc")
        && value.get("source_archive_sha256") == manifest.get("sha256")
        && value.get("llama_cpp_cuda_wheel") == manifest.get("llama_cpp_cuda_wheel")
        && value.get("required_packages") == manifest.get("required_packages")
        && value.get("python_package_wheels") == manifest.get("python_package_wheels")
        && value.get("required_native_dlls") == manifest.get("required_native_dlls")
        && required_names.is_subset(&closure_names)
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PythonExecutionLayout {
    Packaged,
    UnbundledDevelopment,
}

fn path_is_beneath(path: &Path, ancestor: &Path) -> bool {
    let Ok(path) = path.canonicalize() else {
        return false;
    };
    let Ok(ancestor) = ancestor.canonicalize() else {
        return false;
    };
    path.starts_with(ancestor)
}

pub fn classify_python_execution_layout(
    current_exe_path: Option<&Path>,
    manifest_dir: &Path,
) -> PythonExecutionLayout {
    let Some(exe_path) = current_exe_path else {
        return PythonExecutionLayout::Packaged;
    };
    let source_marker = manifest_dir.join("python").join("desktop_runtime_setup.py");
    if source_marker.is_file() && path_is_beneath(exe_path, &manifest_dir.join("target")) {
        PythonExecutionLayout::UnbundledDevelopment
    } else {
        PythonExecutionLayout::Packaged
    }
}

pub fn is_unbundled_development_execution(
    current_exe_path: Option<&Path>,
    manifest_dir: &Path,
) -> bool {
    classify_python_execution_layout(current_exe_path, manifest_dir)
        == PythonExecutionLayout::UnbundledDevelopment
}

pub fn is_packaged_execution(current_exe_path: Option<&Path>, manifest_dir: &Path) -> bool {
    !is_unbundled_development_execution(current_exe_path, manifest_dir)
}

fn bundled_runtime_layout_is_eligible(layout: &ResourceLayoutKind, packaged: bool) -> bool {
    *layout == ResourceLayoutKind::MacOsAppResources
        || *layout == ResourceLayoutKind::WindowsResources
        || *layout == ResourceLayoutKind::TauriResourceDir
        || (*layout == ResourceLayoutKind::DevSourceTree && !packaged)
}

fn canonical_resource_root(root: &Path) -> PathBuf {
    root.canonicalize().unwrap_or_else(|_| root.to_path_buf())
}

fn bundled_runtime_candidate(opts: &PythonLauncherResolutionOptions<'_>) -> Option<PythonLauncher> {
    let root_candidates = resource_root_candidates(
        opts.current_exe_path,
        opts.manifest_dir,
        opts.tauri_resource_dir,
    );

    let mut valid_roots: Vec<PathBuf> = Vec::new();
    let mut seen = std::collections::BTreeSet::new();
    for candidate in root_candidates {
        if !bundled_runtime_layout_is_eligible(&candidate.layout, opts.packaged) {
            continue;
        }
        if !candidate
            .root
            .join(BUNDLED_RUNTIME_RELATIVE_PYTHON)
            .is_file()
        {
            continue;
        }
        let canonical = canonical_resource_root(&candidate.root);
        if seen.insert(canonical) {
            valid_roots.push(candidate.root);
        }
    }

    let root = match valid_roots.as_slice() {
        [root] => root,
        _ => return None,
    };
    Some(PythonLauncher::new(
        root.join(BUNDLED_RUNTIME_RELATIVE_PYTHON)
            .to_string_lossy()
            .to_string(),
        vec![],
        PythonLauncherSource::BundledRuntime,
        bundled_runtime_id(),
    ))
}

fn has_confirmed_unbundled_dev_source_tree(opts: &PythonLauncherResolutionOptions<'_>) -> bool {
    if opts.packaged
        || classify_python_execution_layout(opts.current_exe_path, opts.manifest_dir)
            != PythonExecutionLayout::UnbundledDevelopment
    {
        return false;
    }
    opts.manifest_dir
        .join("python")
        .join("desktop_runtime_setup.py")
        .is_file()
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
    let is_bundled_required_platform = cfg!(target_os = "macos") || cfg!(target_os = "windows");
    let is_macos = cfg!(target_os = "macos")
        || opts
            .current_exe_path
            .map(|p| p.components().any(|c| c.as_os_str() == "Contents"))
            .unwrap_or(false);
    if is_bundled_required_platform || is_macos || opts.packaged {
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
                        match metadata_probe_is_valid(
                            &stdout,
                            &runtime_root,
                            expected_runtime_arch(),
                        ) {
                            Ok(()) => {
                                if cfg!(target_os = "windows")
                                    && !bundled_windows_provenance_is_valid(&runtime_root)
                                {
                                    return Err(launcher_error(
                                        DESKTOP_PYTHON_RUNTIME_INVALID,
                                        PythonLauncherCategory::BundledRuntimeProbeFailed,
                                        Some(&candidate),
                                        opts.packaged,
                                        output_status_code(&output),
                                    ));
                                }
                                Ok(candidate.clone())
                            }
                            Err(category) => Err(launcher_error(
                                DESKTOP_PYTHON_RUNTIME_INVALID,
                                category,
                                Some(&candidate),
                                opts.packaged,
                                output_status_code(&output),
                            )),
                        }
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
    if has_confirmed_unbundled_dev_source_tree(&opts) {
        if let Some(env_candidate) = env_python_candidate(opts.override_var_name) {
            return match env_candidate.command_for_version_check().output() {
                Ok(output) => validate_launcher_with_output(&env_candidate, &output, false, false)
                    .map(|_| env_candidate),
                Err(_) => Err(launcher_error(
                    DESKTOP_PYTHON_OVERRIDE_INVALID,
                    PythonLauncherCategory::OverrideMissing,
                    Some(&env_candidate),
                    false,
                    None,
                )),
            };
        }
        return Err(launcher_error(
            DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING,
            PythonLauncherCategory::SystemRuntimeMissing,
            None,
            false,
            None,
        ));
    }
    Err(launcher_error(
        DESKTOP_PYTHON_DEVELOPMENT_DEPENDENCY_MISSING,
        PythonLauncherCategory::SystemRuntimeMissing,
        None,
        false,
        None,
    ))
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
            if cfg!(target_os = "windows") {
                push_unique_resource_root(
                    &mut candidates,
                    exe_dir.to_path_buf(),
                    ResourceLayoutKind::WindowsResources,
                );
            }
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
            .map(|candidate| {
                format!(
                    "{:?}:{}",
                    candidate.layout,
                    candidate
                        .root
                        .file_name()
                        .and_then(|s| s.to_str())
                        .unwrap_or("<root>")
                )
            })
            .collect::<Vec<_>>()
            .join(", ")
    };
    let attempted_bridge_paths = if bridge_candidates.is_empty() {
        "<none>".into()
    } else {
        bridge_candidates
            .iter()
            .map(|candidate| {
                candidate
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("<script>")
                    .to_string()
            })
            .collect::<Vec<_>>()
            .join(", ")
    };
    let selected_layout = root_candidates
        .first()
        .map(|candidate| format!("{:?}", candidate.layout))
        .unwrap_or_else(|| "<unknown>".into());
    let interpreter = interpreter
        .and_then(|p| Path::new(p).file_name().and_then(|s| s.to_str()))
        .unwrap_or("<unresolved>");
    format!(
        "unable to locate desktop Python bridge script '{script_name}'; selected_layout={selected_layout}; interpreter_basename={interpreter}; attempted_resource_roots=[{attempted_roots}]; attempted_bridge_basenames=[{attempted_bridge_paths}]"
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
    command.remove_env(std::ffi::OsStr::new("PYTHONHOME"));
    command.remove_env(std::ffi::OsStr::new("PYTHONUSERBASE"));
}

const PACKAGED_MUTABLE_ENV_PREFIXES: &[&str] = &["PIP_", "CMAKE_"];
const PACKAGED_MUTABLE_ENV_KEYS: &[&str] = &[
    "TOKEN_PLACE_DESKTOP_PYTHON",
    "TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET",
    "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP",
    "TOKEN_PLACE_DESKTOP_DEV_ALLOW_SOURCE_BUILD",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "FORCE_CMAKE",
];

pub fn sanitize_packaged_python_subprocess_env<C>(command: &mut C)
where
    C: PythonEnvCommand,
{
    for key in PACKAGED_MUTABLE_ENV_KEYS {
        command.remove_env(std::ffi::OsStr::new(key));
    }
    // std::process::Command cannot remove dynamic prefixes without enumerating
    // the parent env; remove every currently inherited pip/CMake variable.
    for (key, _) in std::env::vars_os() {
        let upper = key.to_string_lossy().to_ascii_uppercase();
        if PACKAGED_MUTABLE_ENV_PREFIXES
            .iter()
            .any(|prefix| upper.starts_with(prefix))
        {
            command.remove_env(key);
        }
    }
}

fn import_root_is_confirmed_unbundled_development(import_root: &Path) -> bool {
    import_root
        .join("python")
        .join("desktop_runtime_setup.py")
        .is_file()
        && !import_root
            .join("python-runtime")
            .join("embedded_python_runtime_provenance.json")
            .exists()
}

pub fn configure_python_subprocess_env_for_layout<C>(
    command: &mut C,
    import_root: &Path,
    layout: ResourceLayoutKind,
    packaged: bool,
) where
    C: PythonEnvCommand,
{
    disable_python_user_site(command);
    if packaged || layout != ResourceLayoutKind::DevSourceTree {
        sanitize_packaged_python_subprocess_env(command);
    }
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

pub fn configure_python_subprocess_env<C>(command: &mut C, import_root: &Path)
where
    C: PythonEnvCommand,
{
    let layout = if import_root_is_confirmed_unbundled_development(import_root) {
        ResourceLayoutKind::DevSourceTree
    } else {
        ResourceLayoutKind::TauriResourceDir
    };
    configure_python_subprocess_env_for_layout(command, import_root, layout, false);
}

pub trait PythonEnvCommand {
    fn set_env<K, V>(&mut self, key: K, value: V)
    where
        K: AsRef<std::ffi::OsStr>,
        V: AsRef<std::ffi::OsStr>;
    fn remove_env<K>(&mut self, key: K)
    where
        K: AsRef<std::ffi::OsStr>;
}

impl PythonEnvCommand for Command {
    fn set_env<K, V>(&mut self, key: K, value: V)
    where
        K: AsRef<std::ffi::OsStr>,
        V: AsRef<std::ffi::OsStr>,
    {
        self.env(key, value);
    }

    fn remove_env<K>(&mut self, key: K)
    where
        K: AsRef<std::ffi::OsStr>,
    {
        self.env_remove(key);
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

    fn remove_env<K>(&mut self, key: K)
    where
        K: AsRef<std::ffi::OsStr>,
    {
        self.env_remove(key);
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
    fn bundled_windows_provenance_requires_complete_immutable_identity() {
        let temp = TempDir::new().expect("tempdir");
        let runtime = temp.path();
        let provenance = runtime.join("embedded_python_runtime_provenance.json");
        let manifest = bundled_windows_manifest().expect("manifest");
        let required_dlls = manifest
            .get("required_native_dlls")
            .and_then(|v| v.as_array())
            .expect("required dlls");
        let mut pe_dll_closure: Vec<serde_json::Value> = required_dlls
            .iter()
            .filter_map(|name| name.as_str())
            .map(|name| {
                let file = runtime.join(name);
                std::fs::write(&file, format!("pe:{name}")).expect("write required dll");
                let digest = format!("{:x}", Sha256::digest(std::fs::read(&file).unwrap()));
                serde_json::json!({
                    "name": name,
                    "path": name,
                    "machine": "IMAGE_FILE_MACHINE_AMD64",
                    "imports": [],
                    "sha256": digest,
                })
            })
            .collect();
        std::fs::write(runtime.join("python.exe"), b"python-exe").expect("write python.exe");
        pe_dll_closure.push(serde_json::json!({
            "name": "python.exe",
            "path": "python.exe",
            "machine": "IMAGE_FILE_MACHINE_AMD64",
            "imports": ["python311.dll"],
            "sha256": format!("{:x}", Sha256::digest(std::fs::read(runtime.join("python.exe")).unwrap())),
        }));
        let mut valid = serde_json::json!({
            "runtime_id": "bundled-cpython-3.11-win-x86_64-cu124",
            "cpython_version": manifest.get("cpython_version").cloned().unwrap(),
            "target_triple": manifest.get("target_triple").cloned().unwrap(),
            "source_archive_sha256": manifest.get("sha256").cloned().unwrap(),
            "llama_cpp_cuda_wheel": manifest.get("llama_cpp_cuda_wheel").cloned().unwrap(),
            "required_packages": manifest.get("required_packages").cloned().unwrap(),
            "python_package_wheels": manifest.get("python_package_wheels").cloned().unwrap(),
            "required_native_dlls": manifest.get("required_native_dlls").cloned().unwrap(),
            "pe_dll_closure": pe_dll_closure,
        });
        std::fs::write(&provenance, valid.to_string()).expect("write provenance");
        assert!(bundled_windows_provenance_is_valid(runtime));

        valid["llama_cpp_cuda_wheel"]["flavor"] = serde_json::json!("cpu");
        std::fs::write(&provenance, valid.to_string()).expect("write stale provenance");
        assert!(!bundled_windows_provenance_is_valid(runtime));

        std::fs::write(&provenance, "{not-json").expect("write corrupt provenance");
        assert!(!bundled_windows_provenance_is_valid(runtime));
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
    fn packaged_linux_fails_closed_without_confirmed_bundled_runtime() {
        let err = resolve_python_launcher_resource_aware(PythonLauncherResolutionOptions {
            override_var_name: "TOKEN_PLACE_TEST_PYTHON_NOT_SET",
            tauri_resource_dir: None,
            current_exe_path: None,
            manifest_dir: Path::new(env!("CARGO_MANIFEST_DIR")),
            packaged: true,
        })
        .expect_err("packaged Linux must not fall back to system Python");

        assert_eq!(err.public_code, DESKTOP_PYTHON_RUNTIME_MISSING);
        assert_eq!(err.category, PythonLauncherCategory::BundledRuntimeMissing);
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

    fn write_bundled_runtime(root: &Path) -> PathBuf {
        let runtime = root.join(BUNDLED_RUNTIME_RELATIVE_PYTHON);
        std::fs::create_dir_all(runtime.parent().expect("runtime parent"))
            .expect("create runtime dir");
        std::fs::write(&runtime, b"python").expect("write runtime");
        runtime
    }

    #[test]
    #[cfg(target_os = "windows")]
    fn bundled_runtime_candidate_selects_existing_runtime_not_first_nominal_root() {
        let temp = TempDir::new().expect("tempdir");
        let exe = temp
            .path()
            .join("extract")
            .join("token-place-desktop-tauri.exe");
        std::fs::create_dir_all(exe.parent().expect("exe parent")).expect("create exe dir");
        std::fs::write(&exe, b"exe").expect("write exe");
        let expected_runtime = write_bundled_runtime(exe.parent().expect("exe parent"));
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");
        let opts = PythonLauncherResolutionOptions {
            override_var_name: "TOKEN_PLACE_TEST_PYTHON_NOT_SET",
            tauri_resource_dir: None,
            current_exe_path: Some(&exe),
            manifest_dir: &manifest_dir,
            packaged: true,
        };

        let launcher = bundled_runtime_candidate(&opts).expect("runtime candidate");

        assert_eq!(Path::new(&launcher.program), expected_runtime.as_path());
        assert!(!launcher.program.contains("resources"));
    }

    #[test]
    fn bundled_runtime_candidate_does_not_fall_back_when_packaged_runtime_missing() {
        let temp = TempDir::new().expect("tempdir");
        let exe = temp
            .path()
            .join("extract")
            .join("token-place-desktop-tauri.exe");
        std::fs::create_dir_all(exe.parent().expect("exe parent")).expect("create exe dir");
        std::fs::write(&exe, b"exe").expect("write exe");
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");
        write_bundled_runtime(&manifest_dir);
        let opts = PythonLauncherResolutionOptions {
            override_var_name: "TOKEN_PLACE_TEST_PYTHON_NOT_SET",
            tauri_resource_dir: None,
            current_exe_path: Some(&exe),
            manifest_dir: &manifest_dir,
            packaged: true,
        };

        assert!(bundled_runtime_candidate(&opts).is_none());
    }

    #[test]
    #[cfg(target_os = "windows")]
    fn bundled_runtime_candidate_is_fail_closed_for_distinct_installed_roots() {
        let temp = TempDir::new().expect("tempdir");
        let exe = temp
            .path()
            .join("app")
            .join("token-place-desktop-tauri.exe");
        std::fs::create_dir_all(exe.parent().expect("exe parent")).expect("create exe dir");
        std::fs::write(&exe, b"exe").expect("write exe");
        write_bundled_runtime(exe.parent().expect("exe parent"));
        write_bundled_runtime(&exe.parent().expect("exe parent").join("resources"));
        let manifest_dir = temp
            .path()
            .join("repo")
            .join("desktop-tauri")
            .join("src-tauri");
        let opts = PythonLauncherResolutionOptions {
            override_var_name: "TOKEN_PLACE_TEST_PYTHON_NOT_SET",
            tauri_resource_dir: None,
            current_exe_path: Some(&exe),
            manifest_dir: &manifest_dir,
            packaged: true,
        };

        assert!(bundled_runtime_candidate(&opts).is_none());
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
    fn debug_no_bundle_executable_under_target_is_unbundled_development() {
        let temp = TempDir::new().expect("tempdir");
        let manifest = temp.path().join("src-tauri");
        let marker = manifest.join("python").join("desktop_runtime_setup.py");
        std::fs::create_dir_all(marker.parent().unwrap()).unwrap();
        std::fs::write(&marker, "# marker\n").unwrap();
        let exe = manifest.join("target/debug/token-place");
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        std::fs::write(&exe, "").unwrap();

        assert_eq!(
            classify_python_execution_layout(Some(&exe), &manifest),
            PythonExecutionLayout::UnbundledDevelopment
        );
        assert!(!is_packaged_execution(Some(&exe), &manifest));
    }

    #[test]
    fn installed_executable_python_sibling_shape_remains_packaged() {
        let temp = TempDir::new().expect("tempdir");
        let manifest = temp.path().join("repo/desktop-tauri/src-tauri");
        let marker = manifest.join("python").join("desktop_runtime_setup.py");
        std::fs::create_dir_all(marker.parent().unwrap()).unwrap();
        std::fs::write(&marker, "# marker\n").unwrap();
        let exe = temp.path().join("installed/app/token-place");
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        std::fs::write(&exe, "").unwrap();

        assert_eq!(
            classify_python_execution_layout(Some(&exe), &manifest),
            PythonExecutionLayout::Packaged
        );
        assert!(is_packaged_execution(Some(&exe), &manifest));
    }

    #[test]
    fn tauri_resource_dir_equal_to_manifest_dir_does_not_hide_dev_source_tree() {
        let temp = TempDir::new().expect("tempdir");
        let manifest = temp.path().join("src-tauri");
        let marker = manifest.join("python").join("desktop_runtime_setup.py");
        std::fs::create_dir_all(marker.parent().unwrap()).unwrap();
        std::fs::write(&marker, "# marker\n").unwrap();
        let exe = manifest.join("target/debug/token-place");
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        std::fs::write(&exe, "").unwrap();
        let opts = PythonLauncherResolutionOptions {
            override_var_name: "TOKEN_PLACE_TEST_PYTHON_NOT_SET",
            tauri_resource_dir: Some(&manifest),
            current_exe_path: Some(&exe),
            manifest_dir: &manifest,
            packaged: false,
        };

        assert!(has_confirmed_unbundled_dev_source_tree(&opts));
    }

    #[test]
    #[cfg(unix)]
    fn confirmed_development_selects_explicit_override() {
        use std::os::unix::fs::PermissionsExt;
        let temp = TempDir::new().expect("tempdir");
        let manifest = temp.path().join("src-tauri");
        let marker = manifest.join("python").join("desktop_runtime_setup.py");
        std::fs::create_dir_all(marker.parent().unwrap()).unwrap();
        std::fs::write(&marker, "# marker\n").unwrap();
        let exe = manifest.join("target/debug/token-place");
        std::fs::create_dir_all(exe.parent().unwrap()).unwrap();
        std::fs::write(&exe, "").unwrap();
        let override_python = temp.path().join("python-ok");
        std::fs::write(&override_python, "#!/bin/sh\nprintf 'Python 3.11.13\\n'\n").unwrap();
        let mut perms = std::fs::metadata(&override_python).unwrap().permissions();
        perms.set_mode(0o755);
        std::fs::set_permissions(&override_python, perms).unwrap();
        let var = "TOKEN_PLACE_TEST_CONFIRMED_DEV_PYTHON";
        std::env::set_var(var, &override_python);

        let launcher = resolve_python_launcher_resource_aware(PythonLauncherResolutionOptions {
            override_var_name: var,
            tauri_resource_dir: Some(&manifest),
            current_exe_path: Some(&exe),
            manifest_dir: &manifest,
            packaged: false,
        })
        .expect("dev override selected");
        std::env::remove_var(var);

        assert_eq!(launcher.source, PythonLauncherSource::EnvironmentOverride);
        assert_eq!(Path::new(&launcher.program), override_python.as_path());
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
    fn bridge_resolution_error_omits_raw_paths_and_interpreter() {
        let root = PathBuf::from("/Users/alice/secret/project/resources");
        let bridge = root.join("python").join("compute_node_bridge.py");
        let message = format_bridge_script_resolution_error(
            "compute_node_bridge.py",
            &[ResourceRootCandidate {
                root: root.clone(),
                layout: ResourceLayoutKind::TauriResourceDir,
            }],
            &[bridge],
            Some("/Users/alice/secret/python-runtime/python.exe"),
        );
        assert!(!message.contains("/Users/alice/secret"));
        assert!(message.contains("interpreter_basename=python.exe"));
        assert!(message.contains("TauriResourceDir:resources"));
        assert!(message.contains("compute_node_bridge.py"));
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
    fn configure_python_subprocess_env_sanitizes_packaged_import_root() {
        let temp = TempDir::new().expect("tempdir");
        let root = temp.path().join("Resources");
        std::fs::create_dir_all(root.join("python-runtime")).expect("create runtime dir");
        std::fs::write(
            root.join("python-runtime")
                .join("embedded_python_runtime_provenance.json"),
            "{}",
        )
        .expect("write provenance");
        let mut command = Command::new("python");
        command.env("TOKEN_PLACE_DESKTOP_DEV_ALLOW_SOURCE_BUILD", "1");

        configure_python_subprocess_env(&mut command, &root);

        let removed = command.get_envs().any(|(key, value)| {
            key == "TOKEN_PLACE_DESKTOP_DEV_ALLOW_SOURCE_BUILD" && value.is_none()
        });
        assert!(
            removed,
            "packaged runtime must strip development repair opt-in"
        );
    }

    #[test]
    fn configure_python_subprocess_env_for_layout_sanitizes_packaged_even_with_dev_marker() {
        let temp = TempDir::new().expect("tempdir");
        let root = temp.path().join("Resources");
        std::fs::create_dir_all(root.join("python")).expect("create python dir");
        std::fs::write(
            root.join("python").join("desktop_runtime_setup.py"),
            "# stale packaged copy",
        )
        .expect("write marker");
        let mut command = Command::new("python");
        command.env("TOKEN_PLACE_DESKTOP_DEV_ALLOW_SOURCE_BUILD", "1");
        command.env("FORCE_CMAKE", "1");

        configure_python_subprocess_env_for_layout(
            &mut command,
            &root,
            ResourceLayoutKind::WindowsResources,
            true,
        );

        let removed_keys: std::collections::BTreeSet<_> = command
            .get_envs()
            .filter_map(|(key, value)| {
                value
                    .is_none()
                    .then_some(key.to_string_lossy().into_owned())
            })
            .collect();
        assert!(removed_keys.contains("TOKEN_PLACE_DESKTOP_DEV_ALLOW_SOURCE_BUILD"));
        assert!(removed_keys.contains("FORCE_CMAKE"));
    }

    #[test]
    fn configure_python_subprocess_env_preserves_dev_opt_in_for_confirmed_unbundled_tree() {
        let temp = TempDir::new().expect("tempdir");
        let root = temp.path().join("src-tauri");
        std::fs::create_dir_all(root.join("python")).expect("create python dir");
        std::fs::write(
            root.join("python").join("desktop_runtime_setup.py"),
            "# dev",
        )
        .expect("write dev marker");
        let mut command = Command::new("python");
        command.env("TOKEN_PLACE_DESKTOP_DEV_ALLOW_SOURCE_BUILD", "1");

        configure_python_subprocess_env(&mut command, &root);

        let value = command
            .get_envs()
            .find_map(|(key, value)| {
                (key == "TOKEN_PLACE_DESKTOP_DEV_ALLOW_SOURCE_BUILD")
                    .then_some(value.map(|v| v.to_string_lossy().into_owned()))
            })
            .flatten();
        assert_eq!(value.as_deref(), Some("1"));
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
            r#"{{"version":[3,11,13],"machine":"x86_64","executable":"{}","prefix":"{}"}}"#,
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
