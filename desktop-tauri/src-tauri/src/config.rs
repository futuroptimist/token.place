use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

pub const DEFAULT_RELAY_BASE_URL: &str = "https://token.place";

fn default_relay_base_url() -> String {
    DEFAULT_RELAY_BASE_URL.into()
}

fn default_preferred_mode() -> ComputeMode {
    ComputeMode::Auto
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DesktopConfig {
    #[serde(default)]
    pub model_path: String,
    #[serde(default = "default_relay_base_url")]
    pub relay_base_url: String,
    #[serde(default)]
    pub relay_base_urls: Vec<String>,
    #[serde(default = "default_preferred_mode")]
    pub preferred_mode: ComputeMode,
}

impl DesktopConfig {
    pub fn normalized(mut self) -> Self {
        let relay_base_urls =
            normalize_relay_base_urls(&self.relay_base_urls, &self.relay_base_url);
        let relay_base_url = relay_base_urls
            .first()
            .cloned()
            .unwrap_or_else(default_relay_base_url);
        self.relay_base_urls = relay_base_urls;
        self.relay_base_url = relay_base_url;
        self
    }
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            model_path: String::new(),
            relay_base_url: default_relay_base_url(),
            relay_base_urls: vec![default_relay_base_url()],
            preferred_mode: ComputeMode::Auto,
        }
    }
}

pub fn normalize_relay_base_urls(relay_base_urls: &[String], relay_base_url: &str) -> Vec<String> {
    let mut normalized = Vec::new();
    let has_configured_relay_urls = relay_base_urls
        .iter()
        .any(|candidate| !candidate.trim().is_empty());
    let candidates: Vec<&str> = if has_configured_relay_urls {
        relay_base_urls.iter().map(String::as_str).collect()
    } else {
        vec![relay_base_url]
    };
    for candidate in candidates {
        let trimmed = candidate.trim();
        if trimmed.is_empty() || normalized.iter().any(|existing| existing == trimmed) {
            continue;
        }
        normalized.push(trimmed.to_string());
    }
    if normalized.is_empty() {
        normalized.push(default_relay_base_url());
    }
    normalized
}

#[cfg(test)]
mod tests {
    use super::{normalize_relay_base_urls, DesktopConfig};

    #[test]
    fn desktop_config_defaults_to_token_place_relay() {
        let config = DesktopConfig::default();
        assert_eq!(config.relay_base_url, "https://token.place");
        assert_eq!(config.relay_base_urls, vec!["https://token.place"]);
    }

    #[test]
    fn desktop_config_migrates_legacy_single_relay_url() {
        let config: DesktopConfig = serde_json::from_str::<DesktopConfig>(
            r#"{
                "model_path": "/tmp/model.gguf",
                "relay_base_url": "https://legacy.example",
                "preferred_mode": "auto"
            }"#,
        )
        .expect("legacy config should deserialize")
        .normalized();

        assert_eq!(config.relay_base_url, "https://legacy.example");
        assert_eq!(config.relay_base_urls, vec!["https://legacy.example"]);
    }

    #[test]
    fn desktop_config_normalizes_trims_and_deduplicates_relay_urls() {
        let config: DesktopConfig = serde_json::from_str::<DesktopConfig>(
            r#"{
                "model_path": "/tmp/model.gguf",
                "relay_base_url": " https://relay-one.example ",
                "relay_base_urls": [
                    " https://relay-one.example ",
                    "https://relay-two.example",
                    "",
                    "https://relay-two.example"
                ],
                "preferred_mode": "cpu"
            }"#,
        )
        .expect("config should deserialize")
        .normalized();

        assert_eq!(config.relay_base_url, "https://relay-one.example");
        assert_eq!(
            config.relay_base_urls,
            vec!["https://relay-one.example", "https://relay-two.example"]
        );
    }

    #[test]
    fn normalize_relay_base_urls_uses_default_when_all_urls_are_blank() {
        let relay_urls = normalize_relay_base_urls(&[" ".to_string(), "".to_string()], " ");
        assert_eq!(relay_urls, vec!["https://token.place"]);
    }
}

pub fn config_path(base_dir: &Path) -> PathBuf {
    base_dir.join("desktop_tauri_config.json")
}
