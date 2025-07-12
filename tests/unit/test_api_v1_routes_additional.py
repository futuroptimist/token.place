import base64
import pytest
from relay import app
from api.v1 import routes
from api.v1.validation import ValidationError

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c

def test_get_public_key_exception(client, monkeypatch):
    class Bad:
        @property
        def public_key_b64(self):
            raise RuntimeError("boom")
    monkeypatch.setattr(routes, "encryption_manager", Bad())
    res = client.get("/api/v1/public-key")
    assert res.status_code == 400
    assert "Failed to retrieve public key" in res.get_json()["error"]["message"]

def test_chat_completion_encrypted_validation_error(client, monkeypatch):
    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "model"}])
    monkeypatch.setattr(routes, "get_model_instance", lambda m: object())
    monkeypatch.setattr(routes, "generate_response", lambda m, msgs: msgs)
    def bad(data):
        raise ValidationError("bad", field="f", code="c")
    monkeypatch.setattr(routes, "validate_encrypted_request", bad)
    payload = {
        "model": "model",
        "encrypted": True,
        "client_public_key": base64.b64encode(b"x").decode(),
        "messages": {"ciphertext": "c", "cipherkey": "k", "iv": "i"}
    }
    res = client.post("/api/v1/chat/completions", json=payload)
    data = res.get_json()
    assert res.status_code == 400
    assert data["error"]["param"] == "f"
    assert data["error"]["code"] == "c"

def test_health_check_exception(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("x")

    original = routes.jsonify

    def fake_format(msg, **kw):
        resp = original({"error": {"message": msg}})
        resp.status_code = 400
        return resp

    monkeypatch.setattr(routes, "jsonify", boom)
    monkeypatch.setattr(routes, "format_error_response", fake_format)

    res = client.get("/api/v1/health")
    assert res.status_code == 400
    data = res.get_json()
    assert data["error"]["message"].startswith("Health check failed")
