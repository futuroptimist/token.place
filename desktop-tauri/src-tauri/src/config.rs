use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
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

impl DesktopConfig {
    pub fn with_defaults(mut self) -> Self {
        if self.relay_base_url.trim().is_empty() {
            self.relay_base_url = "https://token.place".into();
        }
        self
    }
}

#[cfg(test)]
mod tests {
    use super::DesktopConfig;

    #[test]
    fn desktop_config_defaults_to_token_place_relay() {
        let config = DesktopConfig::default();
        assert_eq!(config.relay_base_url, "https://token.place");
    }

    #[test]
    fn desktop_config_fills_empty_relay_url() {
        let config = DesktopConfig {
            relay_base_url: String::new(),
            ..DesktopConfig::default()
        }
        .with_defaults();
        assert_eq!(config.relay_base_url, "https://token.place");
    }
}

pub fn config_path(base_dir: &Path) -> PathBuf {
    base_dir.join("desktop_tauri_config.json")
}
