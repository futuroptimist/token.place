use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DesktopConfig {
    pub model_path: String,
    pub relay_base_url: String,
    pub preferred_mode: ComputeMode,
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            model_path: String::new(),
            relay_base_url: "https://token.place".into(),
            preferred_mode: ComputeMode::Auto,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::DesktopConfig;

    #[test]
    fn default_config_targets_token_place_relay() {
        let config = DesktopConfig::default();
        assert_eq!(config.relay_base_url, "https://token.place");
    }
}

pub fn config_path(base_dir: &Path) -> PathBuf {
    base_dir.join("desktop_tauri_config.json")
}
