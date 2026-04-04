use std::process::Stdio;

use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
};

#[tokio::test]
async fn fake_sidecar_streams_started_token_done() {
    let mut child = Command::new("python3")
        .arg("../sidecar/mock_llama_sidecar.py")
        .arg("--model")
        .arg("/tmp/model.gguf")
        .arg("--mode")
        .arg("cpu")
        .arg("--prompt")
        .arg("integration")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn fake sidecar");

    let stdout = child.stdout.take().expect("stdout available");
    let mut lines = BufReader::new(stdout).lines();

    let first = lines
        .next_line()
        .await
        .expect("line io")
        .expect("line exists");
    assert!(first.contains("\"type\": \"started\"") || first.contains("\"type\":\"started\""));

    let mut saw_token = false;
    let mut saw_done = false;
    while let Some(line) = lines.next_line().await.expect("line io") {
        if line.contains("\"type\": \"token\"") || line.contains("\"type\":\"token\"") {
            saw_token = true;
        }
        if line.contains("\"type\": \"done\"") || line.contains("\"type\":\"done\"") {
            saw_done = true;
            break;
        }
    }

    assert!(saw_token);
    assert!(saw_done);
}
