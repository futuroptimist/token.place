"""New N01–N25 reconstruction; not the unavailable historical 25-case matrix."""

import ast
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[3]
TARGETS = (
    "TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL", "TOKENPLACE_DISTRIBUTED_RELAY_URL",
    "TOKENPLACE_DISTRIBUTED_COMPUTE_URL", "TOKENPLACE_RELAY_INTERNAL_URL",
    "TOKEN_PLACE_RELAY_INTERNAL_URL", "RELAY_INTERNAL_URL", "TOKENPLACE_RELAY_PUBLIC_URL",
    "TOKEN_PLACE_RELAY_PUBLIC_URL", "RELAY_PUBLIC_URL",
)


def forbid(*_args, **_kwargs):
    pytest.fail("forbidden distributed activity")


def lock_distributed(monkeypatch, cp, *, include_local=False):
    for name in ("_select_distributed_target", "_validated_target_url", "_normalise_target_url",
                 "_config_value", "_build_api_v1_compute_provider"):
        monkeypatch.setattr(cp, name, Mock(side_effect=forbid))
    monkeypatch.setattr(cp.DistributedApiV1ComputeProvider, "__init__", forbid)
    if include_local:
        monkeypatch.setattr(cp.LocalApiV1ComputeProvider, "__init__", forbid)


def error(response, status, code):
    assert response.status_code == status, response.get_json()
    assert response.get_json()["error"]["code"] == code


def distributed(monkeypatch, target="https://relay.example/api/v1"):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")
    if target:
        monkeypatch.setenv(TARGETS[0], target)


def test_offline_guard_is_mandatory():
    # A missing guard must fail this test; never connect even when absent.
    with pytest.raises(AssertionError, match="K046 offline guard"):
        socket.socket()
    with pytest.raises(AssertionError, match="K046 offline guard"):
        socket.getaddrinfo("never-resolve.example", 80)


@pytest.mark.parametrize("env", ["production", "development"])
@pytest.mark.parametrize("target", [*TARGETS, "config", "absent"])
def test_n01_local_full_bridge_never_reads_targets(routing, payload, monkeypatch, env, target):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", " LOCAL ")
    monkeypatch.setenv("TOKEN_PLACE_ENV", env)
    monkeypatch.setenv(target, "https://opaque.example")
    lock_distributed(monkeypatch, routing.cp, include_local=True)
    error(routing.client.post("/chat", json=payload()), 400, "distributed_routing_disabled")
    routing.local.assert_not_called()


@pytest.mark.parametrize("markers", ["target", "path", "none"])
@pytest.mark.parametrize("encrypted", [True, False])
def test_n02_n03_n06_local_partial_markers_are_inert(routing, payload, monkeypatch, markers, encrypted):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "local")
    for target in TARGETS:
        monkeypatch.setenv(target, "malformed ignored input")
    lock_distributed(monkeypatch, routing.cp)
    response = routing.client.post("/chat", json=payload(encrypted, markers))
    assert response.status_code == 200
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "local"
    routing.local.assert_called_once()


@pytest.mark.parametrize("mode", ["local", "", "distributed"])
@pytest.mark.parametrize("markers", ["both", "target", "none"])
@pytest.mark.parametrize("invalid", ["missing", "base64", "decrypt", "json", "utf8", "messages"])
def test_n04_n12_n20_invalid_envelopes_precede_routing(routing, payload, monkeypatch, mode, markers, invalid):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", mode)
    body = payload(markers=markers)
    if invalid == "missing":
        del body["messages"]["iv"]
    elif invalid == "base64":
        body["messages"]["iv"] = "!"
    else:
        routing.routes.encryption_manager.decrypt_message.return_value = {
            "decrypt": None, "json": b"not json", "utf8": b"\xff", "messages": b'[{"role":"invalid"}]',
        }[invalid]
    monkeypatch.setattr(routing.routes, "resolve_api_v1_routing", forbid)
    lock_distributed(monkeypatch, routing.cp, include_local=True)
    assert routing.client.post("/chat", json=body).status_code == 400


@pytest.mark.parametrize("mode", ["local", "", "distributed"])
def test_n05_n13_n21_plain_full_bridge_rejects_before_routing(routing, payload, monkeypatch, mode):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", mode)
    monkeypatch.setattr(routing.routes, "resolve_api_v1_routing", forbid)
    error(routing.client.post("/chat", json=payload(False)), 400, "encrypted_request_required")


def test_n07_invalid_mode_is_safe_and_reads_no_target(routing, payload, monkeypatch):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "opaque-invalid-value")
    lock_distributed(monkeypatch, routing.cp, include_local=True)
    response = routing.client.post("/chat", json=payload(markers="none"))
    error(response, 503, "invalid_compute_provider_configuration")
    assert "opaque-invalid-value" not in response.get_data(as_text=True)


@pytest.mark.parametrize("mode,code", [("local", "distributed_routing_disabled"), ("", "explicit_distributed_mode_required")])
@pytest.mark.parametrize("offer", ["http://127.0.0.1:5010", "http://lan.example", "https://federation.example"])
def test_n08_mode_helper_cannot_override_policy(routing, monkeypatch, mode, code, offer):
    cp = routing.cp
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", mode)
    lock_distributed(monkeypatch, cp, include_local=True)
    with pytest.raises(cp.ComputeProviderError) as caught:
        cp.get_api_v1_compute_provider_for_mode(mode="distributed", distributed_url=offer)
    assert caught.value.code == code
    with pytest.raises(cp.ComputeProviderError) as caught:
        cp.get_api_v1_distributed_target_selection()
    assert caught.value.code == code


def test_n09_cached_distributed_cannot_survive_local_switch(routing, payload, monkeypatch):
    cp = routing.cp
    distributed(monkeypatch)
    remote = cp.get_api_v1_compute_provider()
    assert isinstance(remote, cp.FallbackApiV1ComputeProvider)
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "local")
    with monkeypatch.context() as patch:
        lock_distributed(patch, cp)
        assert routing.client.post("/chat", json=payload(markers="none")).status_code == 200
        routing.local.side_effect = cp.ComputeProviderError("opaque-private-detail")
        response = routing.client.post("/chat", json=payload(markers="none"))
        assert response.status_code == 502
        assert "opaque-private-detail" not in response.get_data(as_text=True)
    tree = ast.parse((ROOT / "relay.py").read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_enforce_api_v1_distributed_guardrail")
    import os
    namespace = {"os": os, "LOGGER": Mock()}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "relay.py", "exec"), namespace)
    monkeypatch.setenv("TOKENPLACE_API_V1_ENFORCE_RELAY_DISTRIBUTED", "1")
    namespace[function.name]()
    assert os.environ["TOKENPLACE_API_V1_COMPUTE_PROVIDER"] == "local"


@pytest.mark.parametrize("mode", ["", "  "])
def test_n10_unset_bridge_never_selects_target(routing, payload, monkeypatch, mode):
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", mode)
    monkeypatch.setenv(TARGETS[-1], "https://relay.example")
    lock_distributed(monkeypatch, routing.cp, include_local=True)
    error(routing.client.post("/chat", json=payload()), 400, "explicit_distributed_mode_required")


@pytest.mark.parametrize("markers", ["target", "path", "none"])
@pytest.mark.parametrize("encrypted", [True, False])
def test_n11_n13_unset_remains_local(routing, payload, monkeypatch, markers, encrypted):
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv(TARGETS[-1], "https://relay.example")
    monkeypatch.setenv(TARGETS[3], "http://lan.example")
    lock_distributed(monkeypatch, routing.cp)
    response = routing.client.post("/chat", json=payload(encrypted, markers))
    assert response.status_code == 200
    assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "local"


@pytest.mark.parametrize("index", range(len(TARGETS)))
def test_n14_n15_n16_exact_env_alias_precedence(routing, monkeypatch, index):
    distributed(monkeypatch, target="")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1")
    for i in range(index, len(TARGETS)):
        monkeypatch.setenv(TARGETS[i], f"https://relay-{i}.example")
    decision = routing.cp.resolve_api_v1_routing(bridge_intent=True)
    assert decision.target.url == f"https://relay-{index}.example"
    assert decision.target.source.endswith(":" + TARGETS[index])
    assert decision.profile == "bridge" and not decision.fallback
    with pytest.raises(FrozenInstanceError):
        decision.mode = "local"
    provider = routing.cp.get_api_v1_compute_provider(routing_decision=decision)
    assert isinstance(provider, routing.cp.DistributedApiV1ComputeProvider)
    assert provider.base_url == decision.target.url


@pytest.mark.parametrize("key", ["api.relay_url", "relay.server_url"])
def test_n17_config_custom_federation_and_api_base(routing, monkeypatch, key):
    distributed(monkeypatch, target="")
    monkeypatch.setattr(routing.cp, "_config_value", lambda k: "http://federation.example/api/v1/" if k == key else "")
    decision = routing.cp.resolve_api_v1_routing()
    assert decision.target.source == "config:" + key
    provider = routing.cp.get_api_v1_compute_provider(routing_decision=decision).primary
    assert provider._relay_url("/api/v1/relay/servers/next") == "http://federation.example/api/v1/relay/servers/next"


def test_n17_config_api_precedes_relay(routing, monkeypatch):
    distributed(monkeypatch, target="")
    monkeypatch.setattr(routing.cp, "_config_value", lambda key: f"https://{key}.example")
    assert routing.cp.resolve_api_v1_routing().target.source == "config:api.relay_url"


@pytest.mark.parametrize("profile", ["general", "bridge", "trusted_lan"])
def test_n18_production_default_only_in_explicit_general(routing, monkeypatch, profile):
    distributed(monkeypatch, target="")
    monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    monkeypatch.setenv("TOKENPLACE_API_V1_DESTINATION_PROFILE", profile)
    if profile == "general":
        assert routing.cp.resolve_api_v1_routing().target.source == "production_default"
    else:
        with pytest.raises(routing.cp.ComputeProviderError):
            routing.cp.resolve_api_v1_routing()


def test_n19_missing_target_strict_and_generic(routing, payload, monkeypatch):
    distributed(monkeypatch, target="")
    error(routing.client.post("/chat", json=payload()), 400, "untrusted_relay_origin")
    assert isinstance(routing.cp.get_api_v1_compute_provider(), routing.cp.LocalApiV1ComputeProvider)
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
    error(routing.client.post("/chat", json=payload(markers="none")), 503, "distributed_target_configuration_error")


@pytest.mark.parametrize("origin,peer", [("http://127.0.0.1", "127.0.0.1"), ("http://127.0.0.1", "203.0.113.1"), ("http://[::1]", "::1"), ("https://untrusted.example", "203.0.113.1")])
@pytest.mark.parametrize("has_target", [True, False])
def test_n22_request_origin_cannot_supply_or_replace_target(routing, payload, monkeypatch, origin, peer, has_target):
    distributed(monkeypatch, "https://operator.example" if has_target else "")
    calls = []
    def complete(provider, **_kwargs):
        calls.append(provider.base_url)
        return {"role": "assistant", "content": "synthetic"}
    monkeypatch.setattr(routing.cp.DistributedApiV1ComputeProvider, "complete_chat", complete)
    body = payload()
    body["metadata"]["relay_url"] = "https://metadata.example"
    response = routing.client.post("/chat", json=body, base_url=origin,
                                   environ_overrides={"REMOTE_ADDR": peer},
                                   headers={"X-Forwarded-Host": "forwarded.example", "Forwarded": "host=forwarded.example"})
    if has_target:
        assert response.status_code == 200
        assert calls == ["https://operator.example"]
    else:
        error(response, 400, "untrusted_relay_origin")
        assert not calls


@pytest.mark.parametrize("target", ["http://127.0.0.1:5010", "http://[::1]:5010", "http://10.0.0.2", "https://lan.example/api/v1"])
def test_n23_lan_requires_exact_enumeration(routing, monkeypatch, target):
    distributed(monkeypatch, target)
    monkeypatch.setenv("TOKENPLACE_API_V1_DESTINATION_PROFILE", "trusted_lan")
    monkeypatch.setenv("TOKENPLACE_API_V1_TRUSTED_LAN_TARGETS", json.dumps([target]))
    decision = routing.cp.resolve_api_v1_routing()
    assert not decision.fallback and decision.profile == "trusted_lan"
    assert isinstance(routing.cp.get_api_v1_compute_provider(routing_decision=decision), routing.cp.DistributedApiV1ComputeProvider)


@pytest.mark.parametrize("case", ["absent", "public", "default", "conflict", "out-of-profile", "duplicate", "malformed", "public-ip"])
def test_n23_lan_rejects_unauthorized_targets_before_construction(routing, monkeypatch, case):
    distributed(monkeypatch, target="")
    monkeypatch.setenv("TOKENPLACE_API_V1_DESTINATION_PROFILE", "trusted_lan")
    monkeypatch.setenv("TOKENPLACE_API_V1_TRUSTED_LAN_TARGETS", '["http://lan.example"]')
    if case == "public":
        monkeypatch.setenv(TARGETS[-1], "http://lan.example")
    elif case == "default":
        monkeypatch.setenv("TOKEN_PLACE_ENV", "production")
    elif case in {"conflict", "out-of-profile"}:
        monkeypatch.setenv(TARGETS[0], "https://external.example")
        monkeypatch.setenv(TARGETS[3], "http://lan.example")
    elif case in {"duplicate", "malformed", "public-ip"}:
        monkeypatch.setenv(TARGETS[0], "http://lan.example")
        monkeypatch.setenv("TOKENPLACE_API_V1_TRUSTED_LAN_TARGETS", {
            "duplicate": '["http://lan.example", "http://lan.example/"]',
            "malformed": '{', "public-ip": '["https://8.8.8.8"]',
        }[case])
    monkeypatch.setattr(routing.cp, "_build_api_v1_compute_provider", forbid)
    with pytest.raises(routing.cp.ComputeProviderError) as caught:
        routing.cp.get_api_v1_compute_provider()
    assert caught.value.code == "distributed_target_configuration_error"


@pytest.mark.parametrize("code,status", [("no_registered_compute_nodes", 503), ("compute_node_unreachable", 503), ("compute_node_timeout", 504), ("compute_node_request_cancelled", 504), ("compute_node_bridge_timeout", 504), ("compute_node_invalid_payload", 502)])
def test_n24_strict_errors_never_retry_locally_or_resolve_again(routing, payload, monkeypatch, caplog, code, status):
    distributed(monkeypatch)
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1")
    def fail(_self, **_kwargs):
        monkeypatch.setattr(routing.routes, "resolve_api_v1_routing", forbid)
        raise routing.cp._error_from_code(code, message="opaque-private-upstream")
    monkeypatch.setattr(routing.cp.DistributedApiV1ComputeProvider, "complete_chat", fail)
    response = routing.client.post("/chat", json=payload())
    error(response, status, code)
    routing.local.assert_not_called()
    assert "opaque-private-upstream" not in caplog.text + response.get_data(as_text=True)


def test_n24_generic_fallback_remains_explicit(routing, payload, monkeypatch):
    distributed(monkeypatch)
    monkeypatch.setattr(routing.cp.DistributedApiV1ComputeProvider, "complete_chat", Mock(side_effect=routing.cp.ComputeProviderError("synthetic")))
    response = routing.client.post("/chat", json=payload(False, "none"))
    assert response.status_code == 200
    assert response.headers["X-Tokenplace-API-V1-Execution-Backend-Path"] == "fallback_local_in_process"
    routing.local.assert_called_once()


@pytest.mark.parametrize("invalid", ["relative", "https://user:opaque@relay.example", "https://relay.example?opaque", "http://[bad", "http://relay.example:bad"])
def test_invalid_targets_are_safe_service_errors(routing, payload, monkeypatch, caplog, invalid):
    distributed(monkeypatch, invalid)
    response = routing.client.post("/chat", json=payload())
    error(response, 503, "distributed_target_configuration_error")
    assert invalid not in caplog.text + response.get_data(as_text=True)


def test_moderation_precedes_routing(routing, payload, monkeypatch):
    distributed(monkeypatch)
    monkeypatch.setattr(routing.routes, "evaluate_messages_for_policy", lambda _messages: SimpleNamespace(allowed=False, matched_term=None, reason=None))
    monkeypatch.setattr(routing.routes, "resolve_api_v1_routing", forbid)
    error(routing.client.post("/chat", json=payload()), 400, "content_blocked")


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.payload = payload or {}

    def json(self):
        return self.payload


@pytest.mark.parametrize("profile", ["bridge", "trusted_lan"])
@pytest.mark.parametrize("outcome", ["complete", "timeout", "cancelled", "no_nodes", "unreachable"])
@pytest.mark.parametrize("encrypted", [True, False])
def test_n21_n25_ciphertext_transport_bound_to_recorded_target(routing, payload, monkeypatch, caplog, profile, outcome, encrypted):
    cp = routing.cp
    target = "https://lan.example/api/v1"
    distributed(monkeypatch, target)
    monkeypatch.setenv("TOKENPLACE_API_V1_DESTINATION_PROFILE", profile)
    monkeypatch.setenv("TOKENPLACE_API_V1_TRUSTED_LAN_TARGETS", json.dumps([target]))
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "1")
    monkeypatch.setenv("HTTPS_PROXY", "https://unapproved-proxy.example")
    clock = SimpleNamespace(now=100.0)
    # Replace the module binding rather than modifying Python's shared time module.
    monkeypatch.setattr(cp, "time", SimpleNamespace(time=lambda: clock.now, sleep=lambda delay: setattr(clock, "now", clock.now + delay)))
    crypto = SimpleNamespace(public_key_b64="synthetic-ephemeral")
    envelope = {}
    def encrypt(message, recipient):
        assert recipient == "synthetic-compute-key"
        envelope.update(message)
        return {"chat_history": "opaque-ciphertext", "cipherkey": "opaque-key", "iv": "opaque-iv"}
    crypto.encrypt_message = encrypt
    crypto.decrypt_message = lambda _payload: {
        "protocol": "tokenplace_api_v1_relay_e2ee", "request_id": envelope["request_id"],
        "client_public_key": crypto.public_key_b64,
        "api_v1_response": {"message": {"role": "assistant", "content": "synthetic completion"}},
    }
    monkeypatch.setattr(cp.DistributedApiV1ComputeProvider, "_build_request_crypto_manager", lambda _self: crypto)
    calls = []
    def transport(method, url, **kwargs):
        assert url.startswith(target + "/relay/")
        assert 0 < kwargs["timeout"] <= 1
        assert "headers" not in kwargs and "auth" not in kwargs
        assert "synthetic-incoming-secret" not in json.dumps(kwargs)
        assert "synthetic prompt" not in json.dumps(kwargs)
        calls.append((method, url, kwargs))
        if url.endswith("/servers/next"):
            # Simulate configuration changes while in flight: every later hop stays pinned.
            monkeypatch.setenv(TARGETS[0], "https://changed.example")
            if outcome == "unreachable":
                raise cp.requests.RequestException("opaque-upstream-detail")
            return FakeResponse(503, {"error": {"code": "no_registered_compute_nodes"}}) if outcome == "no_nodes" else FakeResponse(200, {"server_public_key": "synthetic-compute-key"})
        if url.endswith("/requests"):
            assert set(kwargs["json"]) >= {"chat_history", "cipherkey", "iv"}
            assert "messages" not in kwargs["json"]
            return FakeResponse(200)
        if url.endswith("/responses/retrieve"):
            if outcome == "timeout":
                return FakeResponse(202)
            if outcome == "cancelled":
                return FakeResponse(410)
            return FakeResponse(200, {"chat_history": "opaque", "cipherkey": "opaque", "iv": "opaque"})
        assert url.endswith("/requests/cancel") and outcome == "timeout"
        return FakeResponse(200)
    if profile == "trusted_lan":
        class Session:
            trust_env = True
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return None
            def request(self, method, url, **kwargs):
                assert self.trust_env is False
                assert kwargs.pop("allow_redirects") is False
                return transport(method, url, **kwargs)
        monkeypatch.setattr(cp.requests, "Session", Session)
    else:
        monkeypatch.setattr(cp.requests, "get", lambda url, **kwargs: transport("get", url, **kwargs))
        monkeypatch.setattr(cp.requests, "post", lambda url, **kwargs: transport("post", url, **kwargs))
    response = routing.client.post("/chat", json=payload(encrypted, "both" if encrypted else "none"),
                                   headers={"Authorization": "Bearer synthetic-incoming-secret", "X-Operator-Token": "synthetic-incoming-secret"})
    if outcome == "complete":
        assert response.status_code == 200
        assert response.headers["X-Tokenplace-API-V1-Stream-Mode"] == "non-streaming"
        assert response.headers["X-Tokenplace-API-V1-Resolved-Provider-Path"] == "distributed"
        assert len(calls) == 3
    else:
        code, status = {"timeout": ("compute_node_timeout", 504), "cancelled": ("compute_node_request_cancelled", 504), "no_nodes": ("no_registered_compute_nodes", 503), "unreachable": ("compute_node_unreachable", 503)}[outcome]
        error(response, status, code)
    cancels = [kwargs["json"] for _, url, kwargs in calls if url.endswith("/requests/cancel")]
    assert len(cancels) == (1 if outcome == "timeout" else 0)
    if cancels:
        submitted = next(kwargs["json"] for _, url, kwargs in calls if url.endswith("/requests"))
        assert cancels[0]["cancel_token"] == submitted["cancel_token"]
        assert cancels[0]["request_id"] == submitted["request_id"]
    routing.local.assert_not_called()
    for private in (target, "opaque-upstream-detail", "synthetic-incoming-secret", "synthetic prompt"):
        assert private not in caplog.text


@pytest.mark.parametrize("path", ["/servers/next", "/requests", "/responses/retrieve", "/requests/cancel"])
def test_n23_every_lan_transport_hop_rejects_redirects(routing, monkeypatch, path):
    cp = routing.cp
    seen = []
    class Session:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def request(self, method, url, **kwargs):
            assert not self.trust_env
            assert kwargs["allow_redirects"] is False
            seen.append(url)
            return FakeResponse(302)
    monkeypatch.setattr(cp.requests, "Session", Session)
    provider = cp.DistributedApiV1ComputeProvider("http://lan.example", destination_profile="trusted_lan")
    with pytest.raises(cp.ComputeProviderError) as caught:
        provider._request("post", "http://lan.example/api/v1/relay" + path, timeout=1)
    assert caught.value.code == "distributed_target_configuration_error"
    assert len(seen) == 1


def test_cache_separates_profile_source_and_fallback(routing, monkeypatch):
    cp = routing.cp
    distributed(monkeypatch)
    general = cp.get_api_v1_compute_provider()
    assert isinstance(general, cp.FallbackApiV1ComputeProvider)
    strict = cp.resolve_api_v1_routing(bridge_intent=True)
    bridge = cp.get_api_v1_compute_provider(routing_decision=strict)
    assert isinstance(bridge, cp.DistributedApiV1ComputeProvider)
    monkeypatch.setenv("TOKENPLACE_API_V1_DESTINATION_PROFILE", "trusted_lan")
    monkeypatch.setenv("TOKENPLACE_API_V1_TRUSTED_LAN_TARGETS", '["https://relay.example/api/v1"]')
    lan = cp.get_api_v1_compute_provider()
    assert lan is not bridge and lan.destination_profile == "trusted_lan"
    assert cp._build_api_v1_compute_provider.cache_info().currsize == 3
    monkeypatch.delenv(TARGETS[0])
    monkeypatch.setenv(TARGETS[3], "https://relay.example/api/v1")
    assert cp.get_api_v1_compute_provider() is not lan


def test_helper_cannot_replace_target_or_broaden_strict_fallback(routing, monkeypatch):
    cp = routing.cp
    distributed(monkeypatch)
    monkeypatch.setenv("TOKENPLACE_API_V1_DESTINATION_PROFILE", "bridge")
    with pytest.raises(cp.ComputeProviderError):
        cp.get_api_v1_compute_provider_for_mode(mode="distributed", distributed_url="https://unapproved.example")
    provider = cp.get_api_v1_compute_provider_for_mode(mode="distributed", distributed_fallback_enabled=True)
    assert isinstance(provider, cp.DistributedApiV1ComputeProvider)


def test_launch_sources_declare_explicit_bridge_without_overriding_local():
    chart = (ROOT / "charts/tokenplace/templates/deployment.yaml").read_text()
    for name, value in (("COMPUTE_PROVIDER", "distributed"), ("DISTRIBUTED_FALLBACK", "0"), ("DESTINATION_PROFILE", "bridge")):
        assert f'"TOKENPLACE_API_V1_{name}" "value" "{value}"' in chart
    assert '"TOKENPLACE_RELAY_INTERNAL_URL" "value" $selfDispatchURL' in chart
    assert chart.index('$defaultEnv := dict') < chart.index('$mergedEnv := deepCopy $defaultEnv') < chart.index('range $key, $val := .Values.env') < chart.index('range $item := .Values.extraEnv')
    integration = (ROOT / "tests/integration/test_api_v1_desktop_bridge_e2ee.py").read_text()
    assert '"get_api_v1_compute_provider_for_mode"' not in integration
    assert 'setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "distributed")' in integration
    assert 'setenv("TOKENPLACE_API_V1_DISTRIBUTED_RELAY_URL", base_url)' in integration
    for file in ("tests/conftest.py", "desktop-tauri/scripts/test_desktop_relay_operator_parity_e2e.py", "tests/integration/test_server_py_compute_node_cli.py"):
        text = (ROOT / file).read_text()
        assert '"TOKENPLACE_API_V1_COMPUTE_PROVIDER"' in text and '"distributed"' in text
        assert '"TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK"' in text


def test_retained_decision_cannot_bypass_explicit_local(routing, monkeypatch):
    cp = routing.cp
    distributed(monkeypatch)
    decision = cp.resolve_api_v1_routing()
    cp.get_api_v1_compute_provider(routing_decision=decision)
    monkeypatch.setenv("TOKENPLACE_API_V1_COMPUTE_PROVIDER", "local")
    lock_distributed(monkeypatch, cp, include_local=True)
    with pytest.raises(cp.ComputeProviderError) as caught:
        cp.get_api_v1_compute_provider(routing_decision=decision)
    assert caught.value.code == "distributed_routing_disabled"


@pytest.mark.parametrize("markers", ["target", "path", "none"])
def test_n16_distributed_partial_markers_do_not_change_destination(routing, payload, monkeypatch, markers):
    distributed(monkeypatch, target="")
    monkeypatch.setenv(TARGETS[-1], "https://public.example")
    monkeypatch.setenv("TOKENPLACE_API_V1_DISTRIBUTED_FALLBACK", "0")
    seen = []
    def complete(provider, **_kwargs):
        seen.append((provider.base_url, provider.destination_profile))
        return {"role": "assistant", "content": "synthetic"}
    monkeypatch.setattr(routing.cp.DistributedApiV1ComputeProvider, "complete_chat", complete)
    response = routing.client.post("/chat", json=payload(markers=markers))
    assert response.status_code == 200
    assert seen == [("https://public.example", "general")]


def test_strict_selection_does_not_construct_unused_local_provider(routing, monkeypatch):
    distributed(monkeypatch)
    monkeypatch.setattr(routing.cp.LocalApiV1ComputeProvider, "__init__", forbid)
    decision = routing.cp.resolve_api_v1_routing(bridge_intent=True)
    assert isinstance(routing.cp.get_api_v1_compute_provider(routing_decision=decision), routing.cp.DistributedApiV1ComputeProvider)


def test_lan_cancel_redirect_cannot_mask_original_timeout(routing, monkeypatch):
    cp = routing.cp
    crypto = SimpleNamespace(public_key_b64="opaque", encrypt_message=lambda *_args: {"chat_history": "opaque", "cipherkey": "opaque", "iv": "opaque"})
    monkeypatch.setattr(cp.DistributedApiV1ComputeProvider, "_build_request_crypto_manager", lambda _self: crypto)
    timestamps = iter([100.0, 100.1, 100.2, 101.2])
    monkeypatch.setattr(cp, "time", SimpleNamespace(time=lambda: next(timestamps)))
    calls = []
    class Session:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def request(self, method, url, **kwargs):
            assert not self.trust_env and kwargs["allow_redirects"] is False
            calls.append(url)
            if url.endswith("/servers/next"):
                return FakeResponse(200, {"server_public_key": "opaque"})
            return FakeResponse(302 if url.endswith("/cancel") else 200)
    monkeypatch.setattr(cp.requests, "Session", Session)
    provider = cp.DistributedApiV1ComputeProvider("http://lan.example", timeout_seconds=1, destination_profile="trusted_lan")
    with pytest.raises(cp.ComputeProviderError) as caught:
        provider.complete_chat(model_id="qwen3-8b-instruct", messages=[{"role": "user", "content": "synthetic"}])
    assert caught.value.code == "compute_node_timeout"
    assert len(calls) == 3 and calls[-1].endswith("/cancel")
