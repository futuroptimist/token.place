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
from encrypt import encrypt, generate_keys
from relay import app

json_module = json


def test_distributed_compute_provider_uses_relay_blind_e2ee_envelope(monkeypatch):
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=5)

    server_private_key, server_public_key = generate_keys()
    captured = {"faucet_payload": None}

    def fake_get(url, timeout):
        assert url == "https://node-a.example/next_server"
        assert timeout == 5

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"server_public_key": base64.b64encode(server_public_key).decode("utf-8")}

        return _Resp()

    def fake_post(url, json=None, timeout=None):
        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        if url.endswith("/faucet"):
            captured["faucet_payload"] = dict(json or {})
            return _Resp({"message": "Request received"})

        assert url.endswith("/retrieve")
        payload = captured["faucet_payload"]
        assert payload is not None

        decrypted_request = compute_provider.decrypt(
            {
                "ciphertext": base64.b64decode(payload["chat_history"]),
                "iv": base64.b64decode(payload["iv"]),
            },
            base64.b64decode(payload["cipherkey"]),
            server_private_key,
        )
        request_obj = json_module.loads(decrypted_request.decode("utf-8"))
        assert request_obj["chat_history"] == [{"role": "user", "content": "hi"}]
        assert request_obj["api_v1_envelope"]["model"] == "llama-3-8b-instruct"

        response_history = request_obj["chat_history"] + [{"role": "assistant", "content": "relay reply"}]
        ciphertext_dict, cipherkey, iv = encrypt(
            json_module.dumps(response_history).encode("utf-8"),
            base64.b64decode(payload["client_public_key"]),
        )
        return _Resp(
            {
                "chat_history": base64.b64encode(ciphertext_dict["ciphertext"]).decode("utf-8"),
                "cipherkey": base64.b64encode(cipherkey).decode("utf-8"),
                "iv": base64.b64encode(iv).decode("utf-8"),
            }
        )

    monkeypatch.setattr(compute_provider.requests, "get", fake_get)
    monkeypatch.setattr(compute_provider.requests, "post", fake_post)
    monkeypatch.setattr(compute_provider.time, "sleep", lambda *_: None)

    assistant = provider.complete_chat(
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert assistant == {"role": "assistant", "content": "relay reply"}


def test_distributed_compute_provider_rejects_options_in_distributed_mode():
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=5)
    try:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
            options={"temperature": 0.2},
        )
        raise AssertionError("expected ComputeProviderError")
    except ComputeProviderError as exc:
        assert exc.code == "distributed_api_v1_options_unsupported"
        assert exc.error_type == "invalid_request_error"
        assert exc.status_code == 400


def test_fallback_compute_provider_uses_local_adapter_when_distributed_disabled(monkeypatch):
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
    monkeypatch.setattr(
        compute_provider.DistributedApiV1ComputeProvider,
        "complete_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ComputeProviderError(
                "no nodes",
                code="no_registered_compute_nodes",
                error_type="service_unavailable_error",
                public_message="No LLM servers are available right now.",
                status_code=503,
            )
        ),
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
