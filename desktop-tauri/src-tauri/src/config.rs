use std::{fs, path::PathBuf};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use crate::backend::ComputeMode;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DesktopConfig {
    pub model_path: String,
    pub relay_base_url: String,
    pub preferred_compute_mode: ComputeMode,
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            model_path: String::new(),
            relay_base_url: String::from("http://127.0.0.1:5001"),
            preferred_compute_mode: ComputeMode::Auto,
        }
    }
}

pub struct DesktopConfigStore {
    path: PathBuf,
}

impl DesktopConfigStore {
    pub fn new(app: &AppHandle) -> Self {
        let base = app
            .path()
            .app_config_dir()
            .expect("app config dir available");
        Self {
            path: base.join("desktop_mvp_config.json"),
        }
    }

    pub fn load(&self) -> Result<DesktopConfig, Box<dyn std::error::Error>> {
        if !self.path.exists() {
            return Ok(DesktopConfig::default());
        }
        let content = fs::read_to_string(&self.path)?;
        Ok(serde_json::from_str(&content)?)
    }

    pub fn save(&self, config: &DesktopConfig) -> Result<(), Box<dyn std::error::Error>> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&self.path, serde_json::to_string_pretty(config)?)?;
        Ok(())
    }
}
