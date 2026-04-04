use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum Backend {
    Metal,
    Cuda,
    Cpu,
}

impl std::fmt::Display for Backend {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Backend::Metal => write!(f, "metal"),
            Backend::Cuda => write!(f, "cuda"),
            Backend::Cpu => write!(f, "cpu"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct BackendDetection {
    pub backend: Backend,
    pub label: String,
    pub reason: String,
}

pub fn detect_backend(os: &str, arch: &str) -> BackendDetection {
    match (os, arch) {
        ("macos", "aarch64") => BackendDetection {
            backend: Backend::Metal,
            label: "Metal / Apple Silicon".to_string(),
            reason: "macOS arm64 defaults to llama.cpp Metal backend".to_string(),
        },
        ("windows", "x86_64") => BackendDetection {
            backend: Backend::Cuda,
            label: "CUDA / NVIDIA".to_string(),
            reason: "Windows x64 defaults to llama.cpp CUDA backend".to_string(),
        },
        _ => BackendDetection {
            backend: Backend::Cpu,
            label: "CPU fallback".to_string(),
            reason: "preferred GPU backend unavailable for this platform".to_string(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::{detect_backend, Backend};

    #[test]
    fn picks_metal_for_macos_arm() {
        let detected = detect_backend("macos", "aarch64");
        assert_eq!(detected.backend, Backend::Metal);
        assert!(detected.label.contains("Apple Silicon"));
    }

    #[test]
    fn picks_cuda_for_windows_x64() {
        let detected = detect_backend("windows", "x86_64");
        assert_eq!(detected.backend, Backend::Cuda);
        assert!(detected.label.contains("NVIDIA"));
    }

    #[test]
    fn falls_back_to_cpu_for_other_platforms() {
        let detected = detect_backend("linux", "x86_64");
        assert_eq!(detected.backend, Backend::Cpu);
        assert_eq!(detected.label, "CPU fallback");
    }
}
