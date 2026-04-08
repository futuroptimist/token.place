use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Command;

const CANONICAL_MODEL_FAMILY_URL: &str = "https://huggingface.co/meta-llama/Meta-Llama-3-8B";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelArtifactMetadata {
    pub filename: String,
    pub url: String,
    pub models_dir: String,
    pub resolved_model_path: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ModelMetadataResponse {
    pub canonical_model_family_url: String,
    pub artifact: ModelArtifactMetadata,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DownloadModelResponse {
    pub ok: bool,
    pub artifact: Option<ModelArtifactMetadata>,
    pub error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PythonDownloadResponse {
    ok: bool,
    artifact: Option<ModelArtifactMetadata>,
    error: Option<String>,
}

fn script_path() -> Result<PathBuf, String> {
    std::env::current_dir()
        .map_err(|e| e.to_string())
        .map(|cwd| cwd.join("../sidecar/model_bridge.py"))
}

fn run_bridge(command: &str) -> Result<String, String> {
    let script = script_path()?;
    if !script.exists() {
        return Err(format!("model bridge script missing: {}", script.display()));
    }

    let python_bin =
        std::env::var("TOKEN_PLACE_SIDECAR_PYTHON").unwrap_or_else(|_| "python3".into());
    let output = Command::new(python_bin)
        .arg(script)
        .arg(command)
        .output()
        .map_err(|e| format!("failed to launch model bridge: {e}"))?;

    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if output.status.success() {
        return Ok(stdout);
    }

    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
    Err(format!(
        "model bridge command failed: {}",
        if stderr.is_empty() { stdout } else { stderr }
    ))
}

pub fn fetch_model_metadata() -> Result<ModelMetadataResponse, String> {
    let output = run_bridge("info")?;
    let mut payload: ModelMetadataResponse = serde_json::from_str(&output)
        .map_err(|e| format!("invalid model metadata response: {e}"))?;

    if payload.canonical_model_family_url.trim().is_empty() {
        payload.canonical_model_family_url = CANONICAL_MODEL_FAMILY_URL.to_string();
    }

    Ok(payload)
}

pub fn download_model() -> Result<DownloadModelResponse, String> {
    let output = run_bridge("download")?;
    let payload: PythonDownloadResponse =
        serde_json::from_str(&output).map_err(|e| format!("invalid download response: {e}"))?;

    Ok(DownloadModelResponse {
        ok: payload.ok,
        artifact: payload.artifact,
        error: payload.error,
    })
}
