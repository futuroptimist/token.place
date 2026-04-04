use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ComputeMode {
    Auto,
    Metal,
    Cuda,
    Cpu,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BackendInfo {
    pub preferred_backend: String,
    pub display_label: String,
    pub platform: String,
}

pub fn detect_backend_info(preferred_mode: ComputeMode) -> BackendInfo {
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;

    let auto_mode = if os == "macos" && arch == "aarch64" {
        ("metal", "Metal / Apple Silicon")
    } else if os == "windows" && arch == "x86_64" {
        ("cuda", "CUDA / NVIDIA")
    } else {
        ("cpu", "CPU fallback")
    };

    let selected = match preferred_mode {
        ComputeMode::Cpu => ("cpu", "CPU fallback"),
        ComputeMode::Metal if os == "macos" && arch == "aarch64" => {
            ("metal", "Metal / Apple Silicon")
        }
        ComputeMode::Cuda if os == "windows" && arch == "x86_64" => ("cuda", "CUDA / NVIDIA"),
        _ => auto_mode,
    };

    BackendInfo {
        preferred_backend: selected.0.to_string(),
        display_label: selected.1.to_string(),
        platform: format!("{os}/{arch}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn detect_for(os: &str, arch: &str, mode: ComputeMode) -> BackendInfo {
        let auto_mode = if os == "macos" && arch == "aarch64" {
            ("metal", "Metal / Apple Silicon")
        } else if os == "windows" && arch == "x86_64" {
            ("cuda", "CUDA / NVIDIA")
        } else {
            ("cpu", "CPU fallback")
        };

        let selected = match mode {
            ComputeMode::Cpu => ("cpu", "CPU fallback"),
            ComputeMode::Metal if os == "macos" && arch == "aarch64" => {
                ("metal", "Metal / Apple Silicon")
            }
            ComputeMode::Cuda if os == "windows" && arch == "x86_64" => ("cuda", "CUDA / NVIDIA"),
            _ => auto_mode,
        };

        BackendInfo {
            preferred_backend: selected.0.to_string(),
            display_label: selected.1.to_string(),
            platform: format!("{os}/{arch}"),
        }
    }

    #[test]
    fn defaults_to_metal_on_macos_apple_silicon() {
        let info = detect_for("macos", "aarch64", ComputeMode::Auto);
        assert_eq!(info.display_label, "Metal / Apple Silicon");
    }

    #[test]
    fn defaults_to_cuda_on_windows_x64() {
        let info = detect_for("windows", "x86_64", ComputeMode::Auto);
        assert_eq!(info.display_label, "CUDA / NVIDIA");
    }

    #[test]
    fn supports_cpu_override() {
        let info = detect_for("macos", "aarch64", ComputeMode::Cpu);
        assert_eq!(info.preferred_backend, "cpu");
    }
}
