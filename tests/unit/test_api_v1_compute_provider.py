from types import SimpleNamespace

import pytest

from api.v1 import compute_provider
from api.v1.compute_provider import (
    ComputeProviderError,
    DistributedApiV1ComputeProvider,
    FallbackApiV1ComputeProvider,
    LocalApiV1ComputeProvider,
    RelayRegisteredApiV1ComputeProvider,
    get_api_v1_resolved_provider_path,
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


def test_relay_registered_provider_uses_next_server_faucet_retrieve(monkeypatch):
    calls = []

    def fake_generate_keys():
        return b"private", b"public"

    def fake_encrypt(plaintext, server_public_key):
        assert server_public_key == b"server-public"
        assert b'"chat_history"' in plaintext
        return {"ciphertext": b"ciphertext"}, b"cipher-key", b"iv"

    assistant_history = (
        b'[{"role":"user","content":"hi"},{"role":"assistant","content":"hello from relay"}]'
    )

    def fake_decrypt(ciphertext_dict, encrypted_key, private_key):
        assert ciphertext_dict["ciphertext"] == b"resp-cipher"
        assert encrypted_key == b"resp-key"
        assert private_key == b"private"
        return assistant_history

    def fake_get(url, timeout=None):
        calls.append(("GET", url))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"server_public_key": "c2VydmVyLXB1YmxpYw=="},
        )

    retrieve_attempts = {"count": 0}

    def fake_post(url, json=None, timeout=None):
        calls.append(("POST", url, json))
        if url.endswith("/faucet"):
            return SimpleNamespace(status_code=200, json=lambda: {"message": "Request received"})
        if url.endswith("/retrieve"):
            retrieve_attempts["count"] += 1
            if retrieve_attempts["count"] == 1:
                return SimpleNamespace(status_code=200, json=lambda: {"error": "No response available for the given public key"})
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "chat_history": "cmVzcC1jaXBoZXI=",
                    "cipherkey": "cmVzcC1rZXk=",
                    "iv": "aXY=",
                },
            )
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("api.v1.compute_provider.generate_keys", fake_generate_keys)
    monkeypatch.setattr("api.v1.compute_provider.encrypt", fake_encrypt)
    monkeypatch.setattr("api.v1.compute_provider.decrypt", fake_decrypt)
    monkeypatch.setattr("api.v1.compute_provider.requests.get", fake_get)
    monkeypatch.setattr("api.v1.compute_provider.requests.post", fake_post)
    monkeypatch.setattr("api.v1.compute_provider.time.sleep", lambda _s: None)

    provider = RelayRegisteredApiV1ComputeProvider(
        base_url="https://relay.example",
        retrieve_max_attempts=2,
        retrieve_retry_seconds=0,
    )
    result = provider.complete_chat(
        model_id="llama-3-8b-instruct",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result == {"role": "assistant", "content": "hello from relay"}
    assert calls[0] == ("GET", "https://relay.example/next_server")
    assert calls[1][0] == "POST" and calls[1][1] == "https://relay.example/faucet"
    assert calls[2][0] == "POST" and calls[2][1] == "https://relay.example/retrieve"


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
    relay_registered = RelayRegisteredApiV1ComputeProvider(base_url="https://relay.example")
    fallback = FallbackApiV1ComputeProvider(primary=distributed, fallback=local)

    assert get_api_v1_resolved_provider_path(local) == "local"
    assert get_api_v1_resolved_provider_path(distributed) == "distributed"
    assert get_api_v1_resolved_provider_path(relay_registered) == "relay_registered"
    assert get_api_v1_resolved_provider_path(fallback) == "distributed_with_local_fallback"
    assert get_api_v1_resolved_provider_path(object()) == "unknown"
