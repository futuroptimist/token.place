use crate::backend::ComputeMode;
use serde::{Deserialize, Deserializer, Serialize};
use std::collections::HashSet;
use std::path::{Path, PathBuf};

pub const DEFAULT_RELAY_BASE_URL: &str = "https://token.place";

#[derive(Debug, Clone, Serialize)]
pub struct DesktopConfig {
    pub model_path: String,
    pub relay_base_url: String,
    pub relay_base_urls: Vec<String>,
    pub preferred_mode: ComputeMode,
}

#[derive(Debug, Deserialize)]
struct DesktopConfigWire {
    #[serde(default)]
    model_path: String,
    #[serde(default = "default_relay_base_url")]
    relay_base_url: String,
    #[serde(default)]
    relay_base_urls: Vec<String>,
    #[serde(default = "default_preferred_mode")]
    preferred_mode: ComputeMode,
}

impl<'de> Deserialize<'de> for DesktopConfig {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let wire = DesktopConfigWire::deserialize(deserializer)?;
        Ok(normalize_desktop_config(DesktopConfig {
            model_path: wire.model_path,
            relay_base_url: wire.relay_base_url,
            relay_base_urls: wire.relay_base_urls,
            preferred_mode: wire.preferred_mode,
        }))
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

fn default_relay_base_url() -> String {
    DEFAULT_RELAY_BASE_URL.into()
}

fn default_preferred_mode() -> ComputeMode {
    ComputeMode::Auto
}

pub fn normalize_relay_base_urls(relay_base_urls: &[String], relay_base_url: &str) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut normalized = Vec::new();

    for relay_url in relay_base_urls {
        let trimmed = relay_url.trim();
        if !trimmed.is_empty() && seen.insert(trimmed.to_string()) {
            normalized.push(trimmed.to_string());
        }
    }

    if normalized.is_empty() {
        let trimmed_legacy = relay_base_url.trim();
        if !trimmed_legacy.is_empty() {
            normalized.push(trimmed_legacy.to_string());
        }
    }

    if normalized.is_empty() {
        normalized.push(DEFAULT_RELAY_BASE_URL.into());
    }

    normalized
}

pub fn normalize_desktop_config(config: DesktopConfig) -> DesktopConfig {
    let relay_base_urls =
        normalize_relay_base_urls(&config.relay_base_urls, &config.relay_base_url);
    let relay_base_url = relay_base_urls
        .first()
        .cloned()
        .unwrap_or_else(default_relay_base_url);

    DesktopConfig {
        model_path: config.model_path,
        relay_base_url,
        relay_base_urls,
        preferred_mode: config.preferred_mode,
    }
}

#[cfg(test)]
mod tests {
    use super::{normalize_desktop_config, normalize_relay_base_urls, DesktopConfig};
    use crate::backend::ComputeMode;

    #[test]
    fn desktop_config_defaults_to_token_place_relay() {
        let config = DesktopConfig::default();
        assert_eq!(config.relay_base_url, "https://token.place");
        assert_eq!(config.relay_base_urls, vec!["https://token.place"]);
    }

    #[test]
    fn desktop_config_migrates_legacy_relay_base_url() {
        let config: DesktopConfig = serde_json::from_str(
            r#"{
                "model_path": "/tmp/model.gguf",
                "relay_base_url": " https://legacy.example ",
                "preferred_mode": "auto"
            }"#,
        )
        .expect("legacy config should deserialize");

        assert_eq!(config.model_path, "/tmp/model.gguf");
        assert_eq!(config.relay_base_url, "https://legacy.example");
        assert_eq!(config.relay_base_urls, vec!["https://legacy.example"]);
    }

    #[test]
    fn desktop_config_normalizes_relay_base_urls() {
        let config = normalize_desktop_config(DesktopConfig {
            model_path: "/tmp/model.gguf".into(),
            relay_base_url: "https://legacy.example".into(),
            relay_base_urls: vec![
                " https://one.example ".into(),
                "https://two.example".into(),
                "https://one.example".into(),
                "  ".into(),
            ],
            preferred_mode: ComputeMode::Auto,
        });

        assert_eq!(config.relay_base_url, "https://one.example");
        assert_eq!(
            config.relay_base_urls,
            vec!["https://one.example", "https://two.example"]
        );
    }

    #[test]
    fn normalize_relay_base_urls_falls_back_to_default() {
        assert_eq!(
            normalize_relay_base_urls(&[" ".into()], " "),
            vec!["https://token.place"]
        );
    }
}

pub fn config_path(base_dir: &Path) -> PathBuf {
    base_dir.join("desktop_tauri_config.json")
}
