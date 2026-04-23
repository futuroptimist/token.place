from types import SimpleNamespace

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


def test_distributed_compute_provider_posts_api_v1_contract(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "distributed response",
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr("api.v1.compute_provider.requests.post", fake_post)

    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example", timeout_seconds=5)
    message = provider.complete_chat(
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
        options={"temperature": 0.2, "stream": True},
    )

    assert captured["url"] == "https://node-a.example/relay/api/v1/chat/completions"
    assert captured["timeout"] == 5
    assert captured["json"]["model"] == "llama-3-8b-instruct"
    assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["json"]["stream"] is False
    assert captured["json"]["temperature"] == 0.2
    assert "stream" not in captured["json"] or captured["json"]["stream"] is False
    assert message["content"] == "distributed response"


def test_fallback_compute_provider_uses_local_adapter_when_distributed_fails(monkeypatch):
    def failing_post(_url, json=None, timeout=None):
        return SimpleNamespace(status_code=503, json=lambda: {"error": "down"})

    monkeypatch.setattr("api.v1.compute_provider.requests.post", failing_post)

    fallback_message = {"role": "assistant", "content": "local fallback"}

    monkeypatch.setattr(
        "api.v1.compute_provider.generate_response",
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


def test_distributed_compute_provider_raises_when_contract_is_invalid(monkeypatch):
    monkeypatch.setattr(
        "api.v1.compute_provider.requests.post",
        lambda _url, json=None, timeout=None: SimpleNamespace(status_code=200, json=lambda: {"choices": []}),
    )

    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example")

    with pytest.raises(ComputeProviderError):
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )


def test_distributed_compute_provider_handles_non_object_error_json(monkeypatch):
    monkeypatch.setattr(
        "api.v1.compute_provider.requests.post",
        lambda _url, json=None, timeout=None: SimpleNamespace(status_code=503, json=lambda: []),
    )
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example")

    with pytest.raises(ComputeProviderError) as exc_info:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert exc_info.value.code == "compute_node_bridge_error"
    assert exc_info.value.status_code == 502


def test_distributed_compute_provider_handles_non_json_error_response(monkeypatch):
    def invalid_json(_url, json=None, timeout=None):
        return SimpleNamespace(status_code=502, json=lambda: (_ for _ in ()).throw(ValueError("invalid json")))

    monkeypatch.setattr("api.v1.compute_provider.requests.post", invalid_json)
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example")

    with pytest.raises(ComputeProviderError) as exc_info:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert exc_info.value.code == "compute_node_bridge_error"
    assert str(exc_info.value) == "distributed provider returned status 502"


def test_distributed_compute_provider_handles_non_object_error_field(monkeypatch):
    monkeypatch.setattr(
        "api.v1.compute_provider.requests.post",
        lambda _url, json=None, timeout=None: SimpleNamespace(
            status_code=503, json=lambda: {"error": "bridge down"}
        ),
    )
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example")

    with pytest.raises(ComputeProviderError) as exc_info:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert exc_info.value.code == "compute_node_bridge_error"
    assert str(exc_info.value) == "distributed provider returned status 503"


def test_distributed_compute_provider_timeout_maps_to_timeout_metadata(monkeypatch):
    def timeout_post(_url, json=None, timeout=None):
        raise compute_provider.requests.Timeout("timed out")

    monkeypatch.setattr("api.v1.compute_provider.requests.post", timeout_post)
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example")

    with pytest.raises(ComputeProviderError) as exc_info:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert exc_info.value.code == "compute_node_bridge_timeout"
    assert exc_info.value.error_type == "timeout_error"
    assert exc_info.value.status_code == 504
    assert exc_info.value.public_message == "The LLM server took too long to respond. Please try again."


def test_distributed_compute_provider_unexpected_exception_maps_to_bridge_error(monkeypatch):
    def boom(_url, json=None, timeout=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("api.v1.compute_provider.requests.post", boom)
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example")

    with pytest.raises(ComputeProviderError) as exc_info:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert exc_info.value.code == "compute_node_bridge_error"
    assert exc_info.value.status_code == 502


def test_distributed_compute_provider_request_exception_maps_to_unreachable(monkeypatch):
    def failing_post(_url, json=None, timeout=None):
        raise compute_provider.requests.RequestException("connection reset by peer")

    monkeypatch.setattr("api.v1.compute_provider.requests.post", failing_post)
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example")

    with pytest.raises(ComputeProviderError) as exc_info:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert exc_info.value.code == "compute_node_unreachable"
    assert exc_info.value.error_type == "service_unavailable_error"
    assert exc_info.value.status_code == 503
    assert exc_info.value.public_message == "The LLM server is unavailable right now. Please try again."


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_error_code"),
    [
        (
            503,
            {
                "error": {
                    "type": "service_unavailable_error",
                    "code": "no_registered_compute_nodes",
                    "message": "No registered compute nodes available",
                }
            },
            "no_registered_compute_nodes",
        ),
        (
            504,
            {
                "error": {
                    "type": "timeout_error",
                    "code": "compute_node_timeout",
                    "message": "Timed out waiting for registered compute node",
                }
            },
            "compute_node_timeout",
        ),
    ],
)
def test_distributed_compute_provider_raises_structured_errors(
    monkeypatch, status_code, payload, expected_error_code
):
    monkeypatch.setattr(
        "api.v1.compute_provider.requests.post",
        lambda _url, json=None, timeout=None: SimpleNamespace(status_code=status_code, json=lambda: payload),
    )
    provider = DistributedApiV1ComputeProvider(base_url="https://node-a.example")

    with pytest.raises(ComputeProviderError) as exc_info:
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )

    assert exc_info.value.code == expected_error_code


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


@pytest.mark.parametrize(
    ("post_status", "expected_backend_path"),
    [
        (200, "registered_desktop_compute_node"),
        (503, "fallback_local_in_process"),
    ],
)
def test_api_v1_chat_completion_emits_execution_backend_path_header(
    monkeypatch, post_status, expected_backend_path
):
    def fake_post(_url, json=None, timeout=None):
        if post_status == 200:
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "distributed response",
                            }
                        }
                    ]
                },
            )
        return SimpleNamespace(status_code=503, json=lambda: {"error": "down"})

    monkeypatch.setattr("api.v1.compute_provider.requests.post", fake_post)
    monkeypatch.setattr(
        "api.v1.compute_provider.generate_response",
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
            == expected_backend_path
        )
    finally:
        compute_provider._build_api_v1_compute_provider.cache_clear()


def test_api_v1_chat_completion_returns_structured_error_for_no_registered_nodes(
    monkeypatch,
):
    monkeypatch.setattr(
        "api.v1.compute_provider.requests.post",
        lambda _url, json=None, timeout=None: SimpleNamespace(
            status_code=503,
            json=lambda: {
                "error": {
                    "type": "service_unavailable_error",
                    "code": "no_registered_compute_nodes",
                    "message": "No registered compute nodes available",
                }
            },
        ),
    )
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
        assert error["code"] == "no_registered_compute_nodes"
        assert error["message"] == "No LLM servers are available right now."
    finally:
        compute_provider._build_api_v1_compute_provider.cache_clear()
