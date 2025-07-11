import base64
import pytest
from relay import app
from api.v1 import routes

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_list_models_exception(client, monkeypatch):
    monkeypatch.setattr(routes, "get_models_info", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    resp = client.get("/api/v1/models")
    assert resp.status_code == 400
    data = resp.get_json()
    assert "error" in data
    assert "Internal server error" in data["error"]["message"]


def test_get_model_exception(client, monkeypatch):
    monkeypatch.setattr(routes, "get_models_info", lambda: (_ for _ in ()).throw(RuntimeError("fail")))
    resp = client.get("/api/v1/models/test-model")
    assert resp.status_code == 400
    assert "Internal server error" in resp.get_json()["error"]["message"]


def test_chat_completion_unexpected_error(client, monkeypatch):
    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "test-model"}])
    monkeypatch.setattr(routes, "get_model_instance", lambda m: object())
    monkeypatch.setattr(routes, "generate_response", lambda m, msgs: (_ for _ in ()).throw(RuntimeError("oops")))
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/api/v1/chat/completions", json=payload)
    assert resp.status_code == 500
    assert "Internal server error" in resp.get_json()["error"]["message"]
