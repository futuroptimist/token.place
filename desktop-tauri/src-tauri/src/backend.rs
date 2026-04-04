use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ComputeMode {
    Auto,
    Metal,
    Cuda,
    Cpu,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum BackendPreference {
    #[serde(rename = "Metal / Apple Silicon")]
    MetalAppleSilicon,
    #[serde(rename = "CUDA / NVIDIA")]
    CudaNvidia,
    #[serde(rename = "CPU fallback")]
    CpuFallback,
}

pub fn detect_backend(os: &str, arch: &str, mode: ComputeMode) -> BackendPreference {
    if mode == ComputeMode::Cpu {
        return BackendPreference::CpuFallback;
    }

    if os == "macos" && arch == "aarch64" {
        return BackendPreference::MetalAppleSilicon;
    }

    if os == "windows" && arch == "x86_64" {
        return BackendPreference::CudaNvidia;
    }

    BackendPreference::CpuFallback
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auto_prefers_metal_for_apple_silicon() {
        assert_eq!(
            detect_backend("macos", "aarch64", ComputeMode::Auto),
            BackendPreference::MetalAppleSilicon
        );
    }

    #[test]
    fn auto_prefers_cuda_for_windows_x64() {
        assert_eq!(
            detect_backend("windows", "x86_64", ComputeMode::Auto),
            BackendPreference::CudaNvidia
        );
    }

    #[test]
    fn cpu_override_is_respected() {
        assert_eq!(
            detect_backend("windows", "x86_64", ComputeMode::Cpu),
            BackendPreference::CpuFallback
        );
    }
}
