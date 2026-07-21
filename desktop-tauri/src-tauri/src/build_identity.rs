use serde::Serialize;

use crate::python_runtime;

#[derive(Debug, Clone, Serialize)]
pub struct BuildIdentity {
    pub app_version: &'static str,
    pub build_id: &'static str,
    pub target_triple: &'static str,
    pub bundled_runtime_id: &'static str,
}

pub fn build_identity() -> BuildIdentity {
    BuildIdentity {
        app_version: env!("CARGO_PKG_VERSION"),
        build_id: env!("TOKEN_PLACE_BUILD_COMMIT"),
        target_triple: target_triple(),
        bundled_runtime_id: python_runtime::bundled_runtime_id(),
    }
}

pub fn target_triple() -> &'static str {
    if cfg!(target_os = "windows") {
        "x86_64-pc-windows-msvc"
    } else if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        "aarch64-apple-darwin"
    } else if cfg!(all(target_os = "macos", target_arch = "x86_64")) {
        "x86_64-apple-darwin"
    } else if cfg!(target_arch = "x86_64") {
        "x86_64-unknown-linux-gnu"
    } else {
        "unknown"
    }
}
