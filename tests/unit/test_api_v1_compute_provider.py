import copy

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
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return copy.deepcopy(self._payload)


class _FakeCryptoManager:
    public_key_b64 = "client-public-key"

    def __init__(self):
        self._encrypted = {}

    def encrypt_message(self, message, _client_public_key):
        token = f"cipher-{len(self._encrypted) + 1}"
        self._encrypted[token] = copy.deepcopy(message)
        return {"chat_history": token, "cipherkey": "encrypted-key", "iv": "encrypted-iv"}

    def decrypt_message(self, encrypted_payload):
        return copy.deepcopy(self._encrypted.get(encrypted_payload.get("chat_history")))


def test_distributed_compute_provider_round_trip_uses_e2ee_envelope(monkeypatch):
    fake_crypto = _FakeCryptoManager()
    posted_payloads = []
    retrieve_calls = []
    retrieve_attempt = {"count": 0}

    def fake_get(url, timeout):
        assert url == "https://node-a.example/next_server"
        assert timeout == 5
        return _FakeResponse(200, {"server_public_key": "server-public-key"})

    def fake_post(url, json, timeout):
        posted_payloads.append((url, copy.deepcopy(json), timeout))
        if url.endswith("/faucet"):
            assert "messages" not in json
            assert "chat_history" in json and json["chat_history"]
            return _FakeResponse(200, {"message": "Request received"})
        if url.endswith("/retrieve"):
            retrieve_calls.append(copy.deepcopy(json))
            retrieve_attempt["count"] += 1
            if retrieve_attempt["count"] == 1:
                return _FakeResponse(
                    200,
                    {
                        "chat_history": "missing-cipher",
                        "cipherkey": "encrypted-key",
                        "iv": "encrypted-iv",
                    },
                )
            if retrieve_attempt["count"] == 2:
                stale_response_envelope = {
                    "protocol": "legacy_protocol",
                    "version": 1,
                    "request_id": "stale",
                    "api_v1_response": {
                        "message": {"role": "assistant", "content": "ignore stale"},
                    },
                }
                stale_encrypted_response = fake_crypto.encrypt_message(
                    stale_response_envelope,
                    fake_crypto.public_key_b64,
                )
                return _FakeResponse(200, stale_encrypted_response)
            response_envelope = {
                "protocol": "tokenplace_api_v1_relay_e2ee",
                "version": 1,
                "request_id": fake_crypto._encrypted["cipher-1"]["request_id"],
                "api_v1_response": {
                    "message": {"role": "assistant", "content": "Distributed secure response"},
                },
            }
            encrypted_response = fake_crypto.encrypt_message(response_envelope, fake_crypto.public_key_b64)
            return _FakeResponse(200, encrypted_response)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(
        compute_provider.DistributedApiV1ComputeProvider,
        "_build_request_crypto_manager",
        lambda _self: fake_crypto,
    )
    monkeypatch.setattr(compute_provider.requests, "get", fake_get)
    monkeypatch.setattr(compute_provider.requests, "post", fake_post)

    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=5)
    response = provider.complete_chat(
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"temperature": 0.2},
    )
    assert response["content"] == "Distributed secure response"
    assert retrieve_calls == [
        {"client_public_key": fake_crypto.public_key_b64},
        {"client_public_key": fake_crypto.public_key_b64},
        {"client_public_key": fake_crypto.public_key_b64},
    ]
    assert posted_payloads[0][0] == "https://node-a.example/faucet"
    assert posted_payloads[1][0] == "https://node-a.example/retrieve"


def test_fallback_compute_provider_uses_local_adapter_when_distributed_fails(monkeypatch):
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
        lambda *_args, **_kwargs: _FakeResponse(200, {"error": {"code": 503}}),
    )

    result = provider.complete_chat(
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hello"}],
    )

    assert result == fallback_message


def test_distributed_compute_provider_maps_faucet_404_to_no_registered_nodes(monkeypatch):
    fake_crypto = _FakeCryptoManager()

    def fake_get(url, timeout):
        assert url == "https://node-a.example/next_server"
        assert timeout == 5
        return _FakeResponse(200, {"server_public_key": "server-public-key"})

    def fake_post(url, json, timeout):
        assert url == "https://node-a.example/faucet"
        return _FakeResponse(404, {"error": "server unavailable"})

    monkeypatch.setattr(
        compute_provider.DistributedApiV1ComputeProvider,
        "_build_request_crypto_manager",
        lambda _self: fake_crypto,
    )
    monkeypatch.setattr(compute_provider.requests, "get", fake_get)
    monkeypatch.setattr(compute_provider.requests, "post", fake_post)

    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=5)
    try:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
        )
        raise AssertionError("expected ComputeProviderError")
    except ComputeProviderError as exc:
        assert exc.code == "no_registered_compute_nodes"
        assert exc.status_code == 503


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
    monkeypatch.setattr(
        compute_provider.requests,
        "get",
        lambda *_args, **_kwargs: _FakeResponse(200, {"error": {"code": 503}}),
    )
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
        assert response.status_code == 503
        error = response.get_json()["error"]
        assert error["type"] == "service_unavailable_error"
        assert error["code"] == "no_registered_compute_nodes"
    finally:
        compute_provider._build_api_v1_compute_provider.cache_clear()
