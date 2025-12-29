import relay


def test_health_reports_public_url(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_PUBLIC_URL", "https://staging.token.place")

    monkeypatch.setattr(relay, "PUBLIC_BASE_URL", relay._load_public_base_url())
    monkeypatch.setitem(relay.app.config, "public_base_url", relay.PUBLIC_BASE_URL)
    monkeypatch.setitem(relay.app.config, "gpu_host", None)

    response = relay.app.test_client().get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["publicUrl"] == "https://staging.token.place"
