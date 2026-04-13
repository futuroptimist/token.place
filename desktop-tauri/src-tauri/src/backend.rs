use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ComputeMode {
    Auto,
    Cpu,
    Gpu,
    Hybrid,
    Metal,
    Cuda,
}

#[derive(Debug, Clone, Serialize)]
pub struct BackendInfo {
    pub platform_label: String,
    pub preferred_mode: ComputeMode,
    pub available_backend: String,
    pub gpu_backend_available: bool,
    pub display_label: String,
}

pub fn detect_backend_for(target_os: &str, target_arch: &str) -> BackendInfo {
    if target_os == "macos" {
        let display_label = if target_arch == "aarch64" {
            "Metal / Apple Silicon"
        } else {
            "Metal / Apple"
        };
        return BackendInfo {
            platform_label: format!("macOS {target_arch}"),
            preferred_mode: ComputeMode::Auto,
            available_backend: "metal".into(),
            gpu_backend_available: true,
            display_label: format!("{display_label} available"),
        };
    }

    if target_os == "windows" && target_arch == "x86_64" {
        return BackendInfo {
            platform_label: "Windows x64".into(),
            preferred_mode: ComputeMode::Auto,
            available_backend: "cuda".into(),
            gpu_backend_available: true,
            display_label: "CUDA / NVIDIA available".into(),
        };
    }

    BackendInfo {
        platform_label: format!("{} {}", target_os, target_arch),
        preferred_mode: ComputeMode::Auto,
        available_backend: "none".into(),
        gpu_backend_available: false,
        display_label: "GPU backend unavailable (CPU only)".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selects_metal_for_apple_silicon() {
        let info = detect_backend_for("macos", "aarch64");
        assert_eq!(info.preferred_mode, ComputeMode::Auto);
        assert_eq!(info.available_backend, "metal");
        assert_eq!(info.display_label, "Metal / Apple Silicon available");
    }

    #[test]
    fn selects_metal_for_macos_intel() {
        let info = detect_backend_for("macos", "x86_64");
        assert_eq!(info.preferred_mode, ComputeMode::Auto);
        assert_eq!(info.available_backend, "metal");
        assert_eq!(info.display_label, "Metal / Apple available");
    }

    #[test]
    fn selects_cuda_for_windows_x64() {
        let info = detect_backend_for("windows", "x86_64");
        assert_eq!(info.preferred_mode, ComputeMode::Auto);
        assert_eq!(info.available_backend, "cuda");
        assert_eq!(info.display_label, "CUDA / NVIDIA available");
    }

    #[test]
    fn falls_back_to_cpu() {
        let info = detect_backend_for("linux", "x86_64");
        assert_eq!(info.preferred_mode, ComputeMode::Auto);
        assert_eq!(info.available_backend, "none");
        assert_eq!(info.display_label, "GPU backend unavailable (CPU only)");
    }
}
