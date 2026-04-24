from api.v1 import compute_provider
from api.v1.compute_provider import (
    ComputeProviderError,
    DistributedApiV1ComputeProvider,
    FallbackApiV1ComputeProvider,
    LocalApiV1ComputeProvider,
    get_api_v1_resolved_provider_path,
)
from relay import app


def test_distributed_compute_provider_uses_e2ee_relay_envelope(monkeypatch):
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=5)
    captured_faucet_payload = {}

    monkeypatch.setattr(
        compute_provider.encryption_manager,
        "encrypt_message",
        lambda payload, _server_key: {
            "encrypted": True,
            "ciphertext": "ciphertext-envelope",
            "cipherkey": "cipherkey-envelope",
            "iv": "iv-envelope",
        },
    )
    monkeypatch.setattr(
        compute_provider.encryption_manager,
        "decrypt_message",
        lambda _ciphertext_dict, _cipherkey: b'{"api_v1_response":{"message":{"role":"assistant","content":"hi"}}}',
    )

    class DummyResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    monkeypatch.setattr(
        compute_provider.requests,
        "get",
        lambda *_args, **_kwargs: DummyResponse({"server_public_key": "server-key-b64"}),
    )

    retrieve_calls = {"count": 0}

    def fake_post(url, **kwargs):
        if url.endswith("/faucet"):
            captured_faucet_payload.update(kwargs["json"])
            return DummyResponse({"message": "ok"})
        if url.endswith("/retrieve"):
            retrieve_calls["count"] += 1
            return DummyResponse(
                {
                    "chat_history": "cmVzcG9uc2UtY2lwaGVydGV4dA==",
                    "cipherkey": "Y2lwaGVya2V5",
                    "iv": "aXY=",
                }
            )
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(compute_provider.requests, "post", fake_post)

    message = provider.complete_chat(
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"temperature": 0.2},
    )

    assert message == {"role": "assistant", "content": "hi"}
    assert "messages" not in captured_faucet_payload
    assert captured_faucet_payload["chat_history"] == "ciphertext-envelope"
    assert retrieve_calls["count"] == 1


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
    compute_provider._build_api_v1_compute_provider.cache_clear()
    class _FailingDistributedProvider:
        def complete_chat(self, **_kwargs):
            raise ComputeProviderError(
                "No LLM servers are available right now.",
                code="no_registered_compute_nodes",
                error_type="service_unavailable_error",
                public_message="No LLM servers are available right now.",
                status_code=503,
            )

    monkeypatch.setattr(
        "api.v1.routes.get_api_v1_compute_provider",
        lambda: _FailingDistributedProvider(),
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
