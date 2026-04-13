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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum BackendKind {
    Cpu,
    Metal,
    Cuda,
}

#[derive(Debug, Clone, Serialize)]
pub struct BackendInfo {
    pub platform_label: String,
    pub available_backend: BackendKind,
    pub availability_label: String,
}

pub fn detect_backend_for(target_os: &str, target_arch: &str) -> BackendInfo {
    if target_os == "macos" {
        let availability_label = if target_arch == "aarch64" {
            "Metal backend available"
        } else {
            "Metal backend available (Intel macOS may perform worse)"
        };
        return BackendInfo {
            platform_label: format!("macOS {target_arch}"),
            available_backend: BackendKind::Metal,
            availability_label: availability_label.into(),
        };
    }

    if target_os == "windows" && target_arch == "x86_64" {
        return BackendInfo {
            platform_label: "Windows x64".into(),
            available_backend: BackendKind::Cuda,
            availability_label: "CUDA backend available".into(),
        };
    }

    BackendInfo {
        platform_label: format!("{} {}", target_os, target_arch),
        available_backend: BackendKind::Cpu,
        availability_label: "No GPU backend detected (CPU only)".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selects_metal_for_apple_silicon() {
        let info = detect_backend_for("macos", "aarch64");
        assert_eq!(info.available_backend, BackendKind::Metal);
        assert_eq!(info.availability_label, "Metal backend available");
    }

    #[test]
    fn selects_metal_for_macos_intel() {
        let info = detect_backend_for("macos", "x86_64");
        assert_eq!(info.available_backend, BackendKind::Metal);
        assert_eq!(
            info.availability_label,
            "Metal backend available (Intel macOS may perform worse)"
        );
    }

    #[test]
    fn selects_cuda_for_windows_x64() {
        let info = detect_backend_for("windows", "x86_64");
        assert_eq!(info.available_backend, BackendKind::Cuda);
        assert_eq!(info.availability_label, "CUDA backend available");
    }

    #[test]
    fn falls_back_to_cpu() {
        let info = detect_backend_for("linux", "x86_64");
        assert_eq!(info.available_backend, BackendKind::Cpu);
        assert_eq!(info.availability_label, "No GPU backend detected (CPU only)");
    }
}
