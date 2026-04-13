use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ComputeMode {
    Auto,
    Cpu,
    #[serde(alias = "metal", alias = "cuda")]
    Gpu,
    Hybrid,
}

#[derive(Debug, Clone, Serialize)]
pub struct BackendInfo {
    pub platform_label: String,
    pub available_backend: String,
    pub gpu_available: bool,
    pub display_label: String,
}

pub fn detect_backend_for(target_os: &str, target_arch: &str) -> BackendInfo {
    if target_os == "macos" {
        let display_label = if target_arch == "aarch64" {
            "Metal available (Apple Silicon)"
        } else {
            "Metal available (macOS)"
        };
        return BackendInfo {
            platform_label: format!("macOS {target_arch}"),
            available_backend: "metal".into(),
            gpu_available: true,
            display_label: display_label.into(),
        };
    }

    if target_os == "windows" && target_arch == "x86_64" {
        return BackendInfo {
            platform_label: "Windows x64".into(),
            available_backend: "cuda".into(),
            gpu_available: true,
            display_label: "CUDA potentially available (NVIDIA)".into(),
        };
    }

    BackendInfo {
        platform_label: format!("{} {}", target_os, target_arch),
        available_backend: "cpu".into(),
        gpu_available: false,
        display_label: "CPU-only host".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selects_metal_for_apple_silicon() {
        let info = detect_backend_for("macos", "aarch64");
        assert!(info.gpu_available);
        assert_eq!(info.available_backend, "metal");
        assert_eq!(info.display_label, "Metal available (Apple Silicon)");
    }

    #[test]
    fn selects_metal_for_macos_intel() {
        let info = detect_backend_for("macos", "x86_64");
        assert!(info.gpu_available);
        assert_eq!(info.available_backend, "metal");
        assert_eq!(info.display_label, "Metal available (macOS)");
    }

    #[test]
    fn selects_cuda_for_windows_x64() {
        let info = detect_backend_for("windows", "x86_64");
        assert!(info.gpu_available);
        assert_eq!(info.available_backend, "cuda");
        assert_eq!(info.display_label, "CUDA potentially available (NVIDIA)");
    }

    #[test]
    fn falls_back_to_cpu() {
        let info = detect_backend_for("linux", "x86_64");
        assert!(!info.gpu_available);
        assert_eq!(info.available_backend, "cpu");
        assert_eq!(info.display_label, "CPU-only host");
    }
}
