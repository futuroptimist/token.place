use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ComputeMode {
    Auto,
    Cpu,
    #[serde(alias = "cuda", alias = "metal")]
    Gpu,
    Hybrid,
}

#[derive(Debug, Clone, Serialize)]
pub struct BackendInfo {
    pub platform_label: String,
    pub preferred_mode: ComputeMode,
    pub available_backend: String,
    pub availability_label: String,
}

pub fn detect_backend_for(target_os: &str, target_arch: &str) -> BackendInfo {
    if target_os == "macos" {
        let availability_label = if target_arch == "aarch64" {
            "Metal-capable platform (Apple Silicon)"
        } else {
            "Metal-capable platform (macOS)"
        };
        return BackendInfo {
            platform_label: format!("macOS {target_arch}"),
            preferred_mode: ComputeMode::Auto,
            available_backend: "metal".into(),
            availability_label: availability_label.into(),
        };
    }

    if target_os == "windows" && target_arch == "x86_64" {
        return BackendInfo {
            platform_label: "Windows x64".into(),
            preferred_mode: ComputeMode::Auto,
            available_backend: "cuda".into(),
            availability_label: "CUDA-capable platform (Windows x64)".into(),
        };
    }

    BackendInfo {
        platform_label: format!("{} {}", target_os, target_arch),
        preferred_mode: ComputeMode::Auto,
        available_backend: "cpu".into(),
        availability_label: "GPU backend not supported on this platform".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selects_metal_availability_for_apple_silicon() {
        let info = detect_backend_for("macos", "aarch64");
        assert_eq!(info.available_backend, "metal");
        assert_eq!(
            info.availability_label,
            "Metal-capable platform (Apple Silicon)"
        );
    }

    #[test]
    fn selects_metal_availability_for_macos_intel() {
        let info = detect_backend_for("macos", "x86_64");
        assert_eq!(info.available_backend, "metal");
        assert_eq!(info.availability_label, "Metal-capable platform (macOS)");
    }

    #[test]
    fn selects_cuda_availability_for_windows_x64() {
        let info = detect_backend_for("windows", "x86_64");
        assert_eq!(info.available_backend, "cuda");
        assert_eq!(
            info.availability_label,
            "CUDA-capable platform (Windows x64)"
        );
    }

    #[test]
    fn falls_back_to_cpu() {
        let info = detect_backend_for("linux", "x86_64");
        assert_eq!(info.available_backend, "cpu");
        assert_eq!(
            info.availability_label,
            "GPU backend not supported on this platform"
        );
    }
}
