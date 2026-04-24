import base64
import json

from api.v1 import compute_provider
from api.v1.compute_provider import (
    ComputeProviderError,
    DistributedApiV1ComputeProvider,
    FallbackApiV1ComputeProvider,
    LocalApiV1ComputeProvider,
    get_api_v1_resolved_provider_path,
)
from relay import app


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_distributed_compute_provider_returns_no_nodes_when_relay_has_no_server(monkeypatch):
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=5)

    monkeypatch.setattr(
        compute_provider.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(
            {"error": {"message": "No servers available", "code": 503}},
            status_code=200,
        ),
    )

    try:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
            options={"temperature": 0.2},
        )
        raise AssertionError("expected ComputeProviderError")
    except ComputeProviderError as exc:
        assert exc.code == "no_registered_compute_nodes"
        assert exc.error_type == "service_unavailable_error"
        assert exc.status_code == 503


def test_fallback_compute_provider_uses_local_adapter_when_distributed_unavailable(monkeypatch):
    fallback_message = {"role": "assistant", "content": "local fallback"}

    monkeypatch.setattr(
        compute_provider,
        "generate_response",
        lambda _model, messages, **_options: messages + [fallback_message],
    )

    provider = FallbackApiV1ComputeProvider(
        primary=DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=0.01),
        fallback=LocalApiV1ComputeProvider(),
    )
    monkeypatch.setattr(
        compute_provider.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(
            {"error": {"message": "No servers available", "code": 503}},
            status_code=200,
        ),
    )

    result = provider.complete_chat(
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
    )

    assert result == fallback_message


def test_get_provider_disables_local_fallback_when_configured(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "https://node-a.example")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")

    compute_provider._build_api_v1_compute_provider.cache_clear()
    try:
        provider = compute_provider.get_api_v1_compute_provider()
        assert isinstance(provider, compute_provider.DistributedApiV1ComputeProvider)
    finally:
        compute_provider._build_api_v1_compute_provider.cache_clear()


def test_get_provider_raises_when_distributed_fallback_disabled_without_url(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")

    compute_provider._build_api_v1_compute_provider.cache_clear()
    try:
        try:
            compute_provider.get_api_v1_compute_provider()
            raise AssertionError("expected ComputeProviderError")
        except ComputeProviderError as exc:
            assert "requires TOKENPLACE_DISTRIBUTED_COMPUTE_URL" in str(exc)
    finally:
        compute_provider._build_api_v1_compute_provider.cache_clear()


def test_get_api_v1_resolved_provider_path_labels_instance_types():
    local = LocalApiV1ComputeProvider()
    distributed = DistributedApiV1ComputeProvider(base_url="https://node-a.example")
    fallback = FallbackApiV1ComputeProvider(primary=distributed, fallback=local)

    assert get_api_v1_resolved_provider_path(local) == "local"
    assert get_api_v1_resolved_provider_path(distributed) == "distributed"
    assert get_api_v1_resolved_provider_path(fallback) == "distributed_with_local_fallback"
    assert get_api_v1_resolved_provider_path(object()) == "unknown"


def test_api_v1_chat_completion_emits_execution_backend_path_header_for_fallback(monkeypatch):
    monkeypatch.setattr(
        compute_provider,
        "generate_response",
        lambda _model, messages, **_options: messages
        + [{"role": "assistant", "content": "fallback response"}],
    )
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "https://node-a.example")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1")
    compute_provider._build_api_v1_compute_provider.cache_clear()

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    try:
        assert response.status_code == 200
        assert (
            response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"]
            == "fallback_local_in_process"
        )
    finally:
        compute_provider._build_api_v1_compute_provider.cache_clear()


def test_api_v1_chat_completion_returns_structured_error_when_distributed_only(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "https://node-a.example")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
    compute_provider._build_api_v1_compute_provider.cache_clear()
    monkeypatch.setattr(
        compute_provider.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(
            {"error": {"message": "No servers available", "code": 503}},
            status_code=200,
        ),
    )

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.post(
            "/api/v1/chat/completions",
            json={
                "model": "llama-3-8b-instruct",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

    try:
        assert response.status_code == 503
        error = response.get_json()["error"]
        assert error["type"] == "service_unavailable_error"
        assert error["code"] == "no_registered_compute_nodes"
    finally:
        compute_provider._build_api_v1_compute_provider.cache_clear()


def test_distributed_compute_provider_uses_relay_blind_encrypted_envelope(monkeypatch):
    provider = DistributedApiV1ComputeProvider(
        base_url="https://node-a.example",
        timeout_seconds=5,
        poll_interval_seconds=0.0,
        response_timeout_seconds=1.0,
    )
    server_private_key, server_public_key = compute_provider.generate_keys()
    _client_private_key, client_public_key = compute_provider.generate_keys()
    assistant_message = {"role": "assistant", "content": "encrypted relay response"}
    network_calls = {"faucet": [], "retrieve": 0}

    def fake_generate_keys():
        return _client_private_key, client_public_key

    def fake_get(url, timeout):
        assert url.endswith("/next_server")
        return _FakeResponse(
            {"server_public_key": base64.b64encode(server_public_key).decode("utf-8")},
            status_code=200,
        )

    def fake_post(url, json, timeout):
        if url.endswith("/faucet"):
            network_calls["faucet"].append(json)
            assert "messages" not in json
            decrypted = compute_provider.decrypt(
                {
                    "ciphertext": base64.b64decode(json["chat_history"]),
                    "iv": base64.b64decode(json["iv"]),
                },
                base64.b64decode(json["cipherkey"]),
                server_private_key,
            )
            payload = json_module.loads(decrypted.decode("utf-8"))
            assert payload["protocol"] == "api_v1_relay_e2ee"
            assert payload["request"]["messages"][0]["content"] == "hello"
            return _FakeResponse({"message": "ok"}, status_code=200)

        if url.endswith("/retrieve"):
            network_calls["retrieve"] += 1
            encrypted_data, encrypted_key, iv = compute_provider.encrypt(
                json_module.dumps({"ok": True, "assistant_message": assistant_message}).encode("utf-8"),
                base64.b64decode(json["client_public_key"]),
                use_pkcs1v15=True,
            )
            return _FakeResponse(
                {
                    "chat_history": base64.b64encode(encrypted_data["ciphertext"]).decode("utf-8"),
                    "cipherkey": base64.b64encode(encrypted_key).decode("utf-8"),
                    "iv": base64.b64encode(iv).decode("utf-8"),
                },
                status_code=200,
            )
        raise AssertionError(f"unexpected url {url}")

    json_module = json
    monkeypatch.setattr(compute_provider, "generate_keys", fake_generate_keys)
    monkeypatch.setattr(compute_provider.requests, "get", fake_get)
    monkeypatch.setattr(compute_provider.requests, "post", fake_post)

    result = provider.complete_chat(
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
        options={"temperature": 0.2},
    )

    assert result == assistant_message
    assert network_calls["faucet"]
    assert network_calls["retrieve"] >= 1
