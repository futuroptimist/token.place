use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const DEFAULT_RELAY_BASE_URL: &str = "https://token.place";
pub const MAX_RELAY_BASE_URLS: usize = 10;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DesktopConfig {
    #[serde(default)]
    pub model_path: String,
    #[serde(default = "default_relay_base_url")]
    pub relay_base_url: String,
    #[serde(default)]
    pub relay_base_urls: Vec<String>,
    #[serde(default = "default_compute_mode")]
    pub preferred_mode: ComputeMode,
}

fn default_relay_base_url() -> String {
    DEFAULT_RELAY_BASE_URL.into()
}

fn default_compute_mode() -> ComputeMode {
    ComputeMode::Auto
}

pub fn normalize_relay_base_urls(
    relay_base_urls: &[String],
    legacy_relay_base_url: &str,
) -> Vec<String> {
    let mut normalized = Vec::new();

    for relay_url in relay_base_urls {
        let trimmed = relay_url.trim();
        if trimmed.is_empty() || normalized.iter().any(|existing| existing == trimmed) {
            continue;
        }
        normalized.push(trimmed.to_string());
        if normalized.len() >= MAX_RELAY_BASE_URLS {
            break;
        }
    }

    if normalized.is_empty() {
        let legacy = legacy_relay_base_url.trim();
        normalized.push(if legacy.is_empty() {
            DEFAULT_RELAY_BASE_URL.into()
        } else {
            legacy.to_string()
        });
    }

    normalized
}

impl DesktopConfig {
    pub fn normalized(mut self) -> Self {
        self.relay_base_urls =
            normalize_relay_base_urls(&self.relay_base_urls, &self.relay_base_url);
        self.relay_base_url = self
            .relay_base_urls
            .first()
            .cloned()
            .unwrap_or_else(default_relay_base_url);
        self
    }
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            model_path: String::new(),
            relay_base_url: DEFAULT_RELAY_BASE_URL.into(),
            relay_base_urls: vec![DEFAULT_RELAY_BASE_URL.into()],
            preferred_mode: ComputeMode::Auto,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{normalize_relay_base_urls, DesktopConfig, DEFAULT_RELAY_BASE_URL};

    #[test]
    fn desktop_config_defaults_to_token_place_relay() {
        let config = DesktopConfig::default();
        assert_eq!(config.relay_base_url, DEFAULT_RELAY_BASE_URL);
        assert_eq!(config.relay_base_urls, vec![DEFAULT_RELAY_BASE_URL]);
    }

    #[test]
    fn legacy_desktop_config_migrates_to_single_relay_list() {
        let config: DesktopConfig = serde_json::from_str::<DesktopConfig>(
            r#"{
                "model_path": "/tmp/model.gguf",
                "relay_base_url": " https://staging.token.place ",
                "preferred_mode": "auto"
            }"#,
        )
        .expect("legacy config should deserialize")
        .normalized();

        assert_eq!(config.relay_base_url, "https://staging.token.place");
        assert_eq!(config.relay_base_urls, vec!["https://staging.token.place"]);
        assert_eq!(config.model_path, "/tmp/model.gguf");
    }

    #[test]
    fn relay_base_url_normalization_trims_dedupes_and_preserves_order() {
        let urls = vec![
            " https://token.place ".to_string(),
            "".to_string(),
            "https://staging.token.place".to_string(),
            "https://token.place".to_string(),
        ];

        assert_eq!(
            normalize_relay_base_urls(&urls, "https://fallback.example"),
            vec!["https://token.place", "https://staging.token.place"]
        );
    }

    #[test]
    fn relay_base_url_normalization_uses_default_when_empty() {
        assert_eq!(
            normalize_relay_base_urls(&[], "   "),
            vec![DEFAULT_RELAY_BASE_URL]
        );
    }
}

pub fn config_path(base_dir: &Path) -> PathBuf {
    base_dir.join("desktop_tauri_config.json")
}
