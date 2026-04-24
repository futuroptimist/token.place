import pytest

from api.v1 import compute_provider
from api.v1.compute_provider import (
    ComputeProviderError,
    DistributedApiV1ComputeProvider,
    FallbackApiV1ComputeProvider,
    LocalApiV1ComputeProvider,
    get_api_v1_resolved_provider_path,
)
from relay import app


def test_distributed_compute_provider_fails_closed_with_plaintext_disabled_error():
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=5)

    with pytest.raises(ComputeProviderError) as exc_info:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hi"}],
            options={"temperature": 0.2},
        )

    assert exc_info.value.code == "relay_api_v1_plaintext_disabled"
    assert exc_info.value.error_type == "service_unavailable_error"
    assert exc_info.value.status_code == 503


def test_fallback_compute_provider_uses_local_adapter_when_distributed_fails_closed(monkeypatch):
    fallback_message = {"role": "assistant", "content": "local fallback"}

    monkeypatch.setattr(
        compute_provider,
        "generate_response",
        lambda _model, messages, **_options: messages + [fallback_message],
    )

    provider = FallbackApiV1ComputeProvider(
        primary=DistributedApiV1ComputeProvider(base_url="https://node-a.example"),
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
        with pytest.raises(ComputeProviderError, match="requires TOKENPLACE_DISTRIBUTED_COMPUTE_URL"):
            compute_provider.get_api_v1_compute_provider()
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


def test_api_v1_chat_completion_emits_fallback_backend_path_header_when_distributed_disabled(
    monkeypatch,
):
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


def test_api_v1_chat_completion_returns_structured_error_when_distributed_disabled_without_fallback(
    monkeypatch,
):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    monkeypatch.setenv("TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "https://node-a.example")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
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
        assert error["code"] == "relay_api_v1_plaintext_disabled"
    finally:
        compute_provider._build_api_v1_compute_provider.cache_clear()
