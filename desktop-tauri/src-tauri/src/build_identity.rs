use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct BuildIdentity {
    pub app_version: &'static str,
    pub build_id: &'static str,
    pub target_triple: &'static str,
    pub bundled_runtime_id: &'static str,
}

pub const BUNDLED_RUNTIME_ID: &str = if cfg!(target_os = "windows") {
    "bundled-cpython-3.11-win-x86_64-cu124"
} else if cfg!(target_os = "macos") {
    "bundled-cpython-3.11-macos-arm64"
} else {
    "bundled-cpython-3.11-unknown"
};

pub fn build_identity() -> BuildIdentity {
    BuildIdentity {
        app_version: env!("CARGO_PKG_VERSION"),
        build_id: option_env!("TOKENPLACE_BUILD_COMMIT").unwrap_or("unknown"),
        target_triple: option_env!("TOKENPLACE_TARGET_TRIPLE").unwrap_or("unknown"),
        bundled_runtime_id: BUNDLED_RUNTIME_ID,
    }
}
