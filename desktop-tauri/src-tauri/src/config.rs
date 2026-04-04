use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AppConfig {
    pub model_path: String,
    pub relay_base_url: String,
    pub compute_mode: String,
}

impl Default for AppConfig {
    fn default() -> Self {
        Self {
            model_path: String::new(),
            relay_base_url: "http://127.0.0.1:5000".to_string(),
            compute_mode: "auto".to_string(),
        }
    }
}

#[cfg(feature = "app")]
mod app_config_io {
    use std::{fs, path::PathBuf};

    use tauri::{AppHandle, Manager};

    use super::AppConfig;

    fn config_path(app: &AppHandle) -> anyhow::Result<PathBuf> {
        let base = app.path().app_config_dir()?;
        Ok(base.join("desktop_mvp_config.json"))
    }

    pub fn load_config(app: &AppHandle) -> anyhow::Result<AppConfig> {
        let path = config_path(app)?;
        if !path.exists() {
            return Ok(AppConfig::default());
        }
        let data = fs::read_to_string(path)?;
        Ok(serde_json::from_str(&data)?)
    }

    pub fn save_config(app: &AppHandle, config: &AppConfig) -> anyhow::Result<()> {
        let path = config_path(app)?;
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(path, serde_json::to_vec_pretty(config)?)?;
        Ok(())
    }
}

#[cfg(feature = "app")]
pub use app_config_io::{load_config, save_config};
