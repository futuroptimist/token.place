use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ComputeMode {
    Auto,
    Metal,
    Cuda,
    Cpu,
}

#[derive(Debug, Clone, Serialize)]
pub struct BackendInfo {
    pub platform_label: String,
    pub preferred_mode: ComputeMode,
    pub display_label: String,
}

pub fn detect_backend_for(target_os: &str, target_arch: &str) -> BackendInfo {
    if target_os == "macos" && target_arch == "aarch64" {
        return BackendInfo {
            platform_label: "macOS arm64".into(),
            preferred_mode: ComputeMode::Metal,
            display_label: "Metal / Apple Silicon".into(),
        };
    }

    if target_os == "windows" && target_arch == "x86_64" {
        return BackendInfo {
            platform_label: "Windows x64".into(),
            preferred_mode: ComputeMode::Cuda,
            display_label: "CUDA / NVIDIA".into(),
        };
    }

    BackendInfo {
        platform_label: format!("{} {}", target_os, target_arch),
        preferred_mode: ComputeMode::Cpu,
        display_label: "CPU fallback".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selects_metal_for_apple_silicon() {
        let info = detect_backend_for("macos", "aarch64");
        assert_eq!(info.preferred_mode, ComputeMode::Metal);
        assert_eq!(info.display_label, "Metal / Apple Silicon");
    }

    #[test]
    fn selects_cuda_for_windows_x64() {
        let info = detect_backend_for("windows", "x86_64");
        assert_eq!(info.preferred_mode, ComputeMode::Cuda);
        assert_eq!(info.display_label, "CUDA / NVIDIA");
    }

    #[test]
    fn falls_back_to_cpu() {
        let info = detect_backend_for("linux", "x86_64");
        assert_eq!(info.preferred_mode, ComputeMode::Cpu);
        assert_eq!(info.display_label, "CPU fallback");
    }
}
