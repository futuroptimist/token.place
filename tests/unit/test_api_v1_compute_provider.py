from types import SimpleNamespace

import pytest

from api.v1.compute_provider import (
    ComputeProviderError,
    DistributedApiV1ComputeProvider,
    FallbackApiV1ComputeProvider,
    LocalApiV1ComputeProvider,
    StrictDistributedApiV1ComputeProvider,
    _build_api_v1_compute_provider,
)


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

    assert captured["url"] == "https://node-a.example/api/v1/chat/completions"
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


def test_build_provider_auto_mode_prefers_distributed_when_url_present():
    provider = _build_api_v1_compute_provider("auto", "https://compute.example", False)
    assert isinstance(provider, StrictDistributedApiV1ComputeProvider)


def test_build_provider_distributed_without_fallback_does_not_call_local(monkeypatch):
    monkeypatch.setattr(
        "api.v1.compute_provider.requests.post",
        lambda _url, json=None, timeout=None: SimpleNamespace(status_code=503, json=lambda: {"error": "down"}),
    )

    local_called = {"value": False}

    def _should_not_call_local(_model, messages, **_kwargs):
        local_called["value"] = True
        return messages

    monkeypatch.setattr("api.v1.compute_provider.generate_response", _should_not_call_local)

    provider = _build_api_v1_compute_provider("distributed", "https://compute.example", False)
    with pytest.raises(ComputeProviderError):
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": "hello"}],
        )
    assert local_called["value"] is False
