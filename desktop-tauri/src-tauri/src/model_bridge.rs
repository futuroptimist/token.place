use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelArtifactMetadata {
    pub canonical_family_url: String,
    pub artifact_file_name: String,
    pub artifact_url: String,
    pub model_path: String,
    pub models_dir: String,
    pub is_downloaded: bool,
}

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .to_path_buf()
}

fn bridge_script_path() -> PathBuf {
    if let Ok(from_env) = std::env::var("TOKEN_PLACE_MODEL_BRIDGE") {
        return PathBuf::from(from_env);
    }
    repo_root().join("utils/llm/desktop_model_bridge.py")
}

fn run_bridge(action: &str) -> Result<ModelArtifactMetadata, String> {
    let python_bin = std::env::var("TOKEN_PLACE_SIDECAR_PYTHON")
        .or_else(|_| std::env::var("TOKEN_PLACE_MODEL_BRIDGE_PYTHON"))
        .unwrap_or_else(|_| "python3".to_string());
    let script = bridge_script_path();
    let output = Command::new(&python_bin)
        .arg(script)
        .arg(action)
        .current_dir(repo_root())
        .output()
        .map_err(|e| format!("failed to invoke python bridge ({python_bin}): {e}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let details = if stderr.is_empty() { stdout } else { stderr };
        return Err(format!("model bridge failed: {details}"));
    }

    serde_json::from_slice::<ModelArtifactMetadata>(&output.stdout)
        .map_err(|e| format!("failed to parse model metadata JSON: {e}"))
}

pub fn load_model_metadata() -> Result<ModelArtifactMetadata, String> {
    run_bridge("metadata")
}

pub fn download_model_artifact() -> Result<ModelArtifactMetadata, String> {
    run_bridge("download")
}
