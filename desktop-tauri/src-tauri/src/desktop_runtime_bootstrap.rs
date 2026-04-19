use crate::backend::ComputeMode;

pub const ENABLE_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP";
pub const DISABLE_BOOTSTRAP_ENV: &str = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP";
pub const REQUIRE_GPU_RUNTIME_ENV: &str = "TOKEN_PLACE_DESKTOP_REQUIRE_GPU_RUNTIME";

fn gpu_runtime_mode(mode: &ComputeMode) -> bool {
    matches!(
        mode,
        ComputeMode::Auto | ComputeMode::Gpu | ComputeMode::Hybrid
    )
}

pub fn should_provision_gpu_runtime_for_desktop(
    mode: &ComputeMode,
    target_os: &str,
    target_arch: &str,
    disable_bootstrap: Option<&str>,
) -> bool {
    if !gpu_runtime_mode(mode) {
        return false;
    }
    if target_os != "windows" || target_arch != "x86_64" {
        return false;
    }
    disable_bootstrap != Some("1")
}

pub fn should_provision_gpu_runtime(mode: &ComputeMode) -> bool {
    should_provision_gpu_runtime_for_desktop(
        mode,
        std::env::consts::OS,
        std::env::consts::ARCH,
        std::env::var(DISABLE_BOOTSTRAP_ENV).ok().as_deref(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enables_bootstrap_for_windows_x64_gpu_modes() {
        assert!(should_provision_gpu_runtime_for_desktop(
            &ComputeMode::Auto,
            "windows",
            "x86_64",
            None
        ));
        assert!(should_provision_gpu_runtime_for_desktop(
            &ComputeMode::Gpu,
            "windows",
            "x86_64",
            None
        ));
        assert!(should_provision_gpu_runtime_for_desktop(
            &ComputeMode::Hybrid,
            "windows",
            "x86_64",
            None
        ));
    }

    #[test]
    fn disables_bootstrap_for_cpu_mode_non_windows_or_explicit_opt_out() {
        assert!(!should_provision_gpu_runtime_for_desktop(
            &ComputeMode::Cpu,
            "windows",
            "x86_64",
            None
        ));
        assert!(!should_provision_gpu_runtime_for_desktop(
            &ComputeMode::Auto,
            "linux",
            "x86_64",
            None
        ));
        assert!(!should_provision_gpu_runtime_for_desktop(
            &ComputeMode::Auto,
            "windows",
            "x86_64",
            Some("1")
        ));
    }
}
