use crate::backend::ComputeMode;
use anyhow::Result;
use serde::{Deserialize, Serialize};
use std::{fs, path::PathBuf};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DesktopConfig {
    pub model_path: Option<String>,
    pub relay_base_url: String,
    pub compute_mode: ComputeMode,
    pub sidecar_command: String,
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            model_path: None,
            relay_base_url: "http://127.0.0.1:5000".to_string(),
            compute_mode: ComputeMode::Auto,
            sidecar_command: "python3 ../fake_sidecar.py".to_string(),
        }
    }
}

pub fn config_path() -> PathBuf {
    let mut path = dirs::config_dir().unwrap_or_else(|| PathBuf::from("."));
    path.push("token.place");
    let _ = fs::create_dir_all(&path);
    path.push("desktop-tauri.json");
    path
}

pub fn load_config() -> DesktopConfig {
    let path = config_path();
    match fs::read_to_string(path) {
        Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
        Err(_) => DesktopConfig::default(),
    }
}

pub fn save_config(config: &DesktopConfig) -> Result<()> {
    let path = config_path();
    let json = serde_json::to_string_pretty(config)?;
    fs::write(path, json)?;
    Ok(())
}
