use crate::backend::ComputeMode;
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

pub const DEFAULT_RELAY_BASE_URL: &str = "https://token.place";
pub const MAX_RELAY_BASE_URLS: usize = 10;

fn default_relay_base_url() -> String {
    DEFAULT_RELAY_BASE_URL.into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DesktopConfig {
    pub model_path: String,
    #[serde(default = "default_relay_base_url")]
    pub relay_base_url: String,
    #[serde(default)]
    pub relay_base_urls: Vec<String>,
    pub preferred_mode: ComputeMode,
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

pub fn normalize_relay_base_urls(
    relay_base_urls: &[String],
    legacy_relay_base_url: &str,
) -> Vec<String> {
    let candidates: Vec<&str> = if relay_base_urls.is_empty() {
        vec![legacy_relay_base_url]
    } else {
        relay_base_urls.iter().map(String::as_str).collect()
    };
    let mut seen = HashSet::new();
    let mut normalized = Vec::new();
    for candidate in candidates {
        let trimmed = candidate.trim();
        if trimmed.is_empty() || !seen.insert(trimmed.to_string()) {
            continue;
        }
        normalized.push(trimmed.to_string());
        if normalized.len() >= MAX_RELAY_BASE_URLS {
            break;
        }
    }
    if normalized.is_empty() {
        normalized.push(DEFAULT_RELAY_BASE_URL.into());
    }
    normalized
}

pub fn normalize_desktop_config(mut config: DesktopConfig) -> DesktopConfig {
    config.relay_base_urls =
        normalize_relay_base_urls(&config.relay_base_urls, &config.relay_base_url);
    config.relay_base_url = config
        .relay_base_urls
        .first()
        .cloned()
        .unwrap_or_else(default_relay_base_url);
    config
}

#[cfg(test)]
mod tests {
    use super::{
        normalize_desktop_config, normalize_relay_base_urls, DesktopConfig, DEFAULT_RELAY_BASE_URL,
    };

    #[test]
    fn desktop_config_defaults_to_token_place_relay() {
        let config = DesktopConfig::default();
        assert_eq!(config.relay_base_url, DEFAULT_RELAY_BASE_URL);
        assert_eq!(
            config.relay_base_urls,
            vec![DEFAULT_RELAY_BASE_URL.to_string()]
        );
    }

    #[test]
    fn legacy_config_migrates_from_single_relay_base_url() {
        let raw = r#"{
            "model_path": "/tmp/model.gguf",
            "relay_base_url": " https://legacy.example ",
            "preferred_mode": "auto"
        }"#;
        let config: DesktopConfig = serde_json::from_str(raw).expect("legacy config deserializes");
        let normalized = normalize_desktop_config(config);
        assert_eq!(
            normalized.relay_base_urls,
            vec!["https://legacy.example".to_string()]
        );
        assert_eq!(normalized.relay_base_url, "https://legacy.example");
        assert_eq!(normalized.model_path, "/tmp/model.gguf");
    }

    #[test]
    fn relay_list_normalization_trims_dedupes_and_preserves_order() {
        let relays = vec![
            " https://one.example ".to_string(),
            "".to_string(),
            "https://two.example".to_string(),
            "https://one.example".to_string(),
        ];
        assert_eq!(
            normalize_relay_base_urls(&relays, "https://legacy.example"),
            vec![
                "https://one.example".to_string(),
                "https://two.example".to_string()
            ]
        );
    }

    #[test]
    fn empty_relay_list_falls_back_to_default() {
        assert_eq!(
            normalize_relay_base_urls(&[], "   "),
            vec![DEFAULT_RELAY_BASE_URL.to_string()]
        );
    }
}

pub fn config_path(base_dir: &Path) -> PathBuf {
    base_dir.join("desktop_tauri_config.json")
}
