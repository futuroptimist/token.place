use serde::Serialize;

pub const BUNDLED_RUNTIME_ID: &str = if cfg!(target_os = "windows") {
    "bundled-cpython-3.11-win-x86_64-cu124"
} else if cfg!(target_os = "macos") {
    "bundled-cpython-3.11-macos-arm64"
} else {
    "bundled-cpython-3.11-unknown"
};

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct BuildIdentity {
    pub app_version: &'static str,
    pub build_id: &'static str,
    pub target_triple: &'static str,
    pub bundled_runtime_id: &'static str,
}

pub fn build_identity() -> BuildIdentity {
    BuildIdentity {
        app_version: env!("CARGO_PKG_VERSION"),
        build_id: option_env!("TOKEN_PLACE_BUILD_ID")
            .or(option_env!("GIT_SHORT_COMMIT"))
            .unwrap_or("dev"),
        target_triple: option_env!("TARGET").unwrap_or("unknown-target"),
        bundled_runtime_id: BUNDLED_RUNTIME_ID,
    }
}
