use token_place_desktop_tauri_lib::sidecar::SidecarManager;

#[tokio::test]
async fn fake_sidecar_streams_until_done() {
    let mut manager = SidecarManager::default();
    let run_id = manager
        .start(
            "model.gguf".to_string(),
            "integration test".to_string(),
            "cpu".to_string(),
        )
        .await
        .expect("starts sidecar");
    let mut rx = manager.subscribe().expect("receiver available");

    let mut saw_token = false;
    let mut saw_done = false;
    while let Some(event) = rx.recv().await {
        if event.run_id != run_id {
            continue;
        }
        if event.event_type == "token" {
            saw_token = true;
        }
        if event.event_type == "done" {
            saw_done = true;
            break;
        }
    }

    assert!(saw_token);
    assert!(saw_done);
}
