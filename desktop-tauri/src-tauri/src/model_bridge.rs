use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Command;

const DESKTOP_BRIDGE_MODULE: &str = "utils.llm.desktop_model_bridge";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelMetadata {
    pub canonical_model_family_url: String,
    pub artifact_filename: String,
    pub artifact_url: String,
    pub resolved_model_path: String,
    pub models_dir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelDownloadResult {
    pub resolved_model_path: String,
    pub artifact_filename: String,
    pub artifact_url: String,
    pub status: String,
}

#[derive(Debug, Deserialize)]
struct BridgeResponse<T> {
    ok: bool,
    data: Option<T>,
    error: Option<String>,
}

fn repo_root() -> anyhow::Result<PathBuf> {
    let tauri_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    tauri_dir
        .parent()
        .and_then(|path| path.parent())
        .map(|path| path.to_path_buf())
        .ok_or_else(|| anyhow::anyhow!("unable to resolve repository root"))
}

fn run_bridge<T>(action: &str) -> anyhow::Result<T>
where
    T: for<'de> Deserialize<'de>,
{
    let root = repo_root()?;
    let output = Command::new("python")
        .arg("-m")
        .arg(DESKTOP_BRIDGE_MODULE)
        .arg(action)
        .current_dir(&root)
        .output()?;

    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();

    if !output.status.success() {
        if let Ok(parsed) = serde_json::from_str::<BridgeResponse<T>>(&stdout) {
            if let Some(error) = parsed.error {
                return Err(anyhow::anyhow!(error));
            }
        }
        let details = if !stderr.is_empty() { stderr } else { stdout };
        return Err(anyhow::anyhow!(
            "model bridge failed (action={action}): {details}"
        ));
    }

    let parsed: BridgeResponse<T> = serde_json::from_str(&stdout)
        .map_err(|e| anyhow::anyhow!("invalid model bridge response: {e}; stdout={stdout}"))?;
    if !parsed.ok {
        return Err(anyhow::anyhow!(parsed
            .error
            .unwrap_or_else(|| "model bridge returned not ok".to_string())));
    }

    parsed
        .data
        .ok_or_else(|| anyhow::anyhow!("model bridge returned missing payload"))
}

pub fn fetch_model_metadata() -> anyhow::Result<ModelMetadata> {
    run_bridge("metadata")
}

pub fn download_model() -> anyhow::Result<ModelDownloadResult> {
    run_bridge("download")
}
