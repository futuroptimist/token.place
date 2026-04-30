import copy
import json
import logging
import re
from pathlib import Path

import pytest

import relay
from api.v1 import compute_provider

E2EE_SENTINEL_RELAY_STATE = "E2EE_SENTINEL_SHOULD_NEVER_REACH_RELAY_STATE"
E2EE_SENTINEL_NETWORK = "E2EE_SENTINEL_SHOULD_NEVER_LEAVE_PROCESS_AS_PLAINTEXT"
E2EE_SENTINEL_LOGS = "E2EE_SENTINEL_SHOULD_NEVER_APPEAR_IN_LOGS"


@pytest.fixture
def relay_client():
    relay.app.config["TESTING"] = True
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    relay.streaming_sessions.clear()
    relay.streaming_sessions_by_client.clear()
    with relay.app.test_client() as client:
        yield client
    relay.known_servers.clear()
    relay.client_inference_requests.clear()
    relay.client_responses.clear()
    relay.streaming_sessions.clear()
    relay.streaming_sessions_by_client.clear()


def _to_text(value):
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except TypeError:
        return repr(value)


def test_static_forbidden_plaintext_patterns_regression_guard():
    root = Path(__file__).resolve().parents[2]
    targets = [
        root / "relay.py",
        root / "api" / "v1" / "compute_provider.py",
        root / "api" / "v1" / "routes.py",
        root / "server.py",
    ]
    forbidden_patterns = [
        r"client_inference_requests\.setdefault\(server_public_key,\s*\[\]\)\.append\(\s*\{[^}]*api_v1_request[^}]*messages",
        r"requests\.post\(self\._relay_url\((?:'|\")/relay/api/v1/chat/completions(?:'|\")\)",
    ]

    scanned = "\n\n".join(f.read_text(encoding="utf-8") for f in targets if f.exists())

    matched = [pattern for pattern in forbidden_patterns if re.search(pattern, scanned, flags=re.DOTALL)]
    assert matched == [], (
        "Found forbidden plaintext relay-dispatch pattern(s): "
        f"{matched}. These patterns indicate PR #813-style regression risk."
    )


def test_runtime_relay_state_never_stores_plaintext_sentinel_when_distributed_disabled(relay_client):
    payload = {
        "model": "llama-3",
        "messages": [{"role": "user", "content": E2EE_SENTINEL_RELAY_STATE}],
    }
    response = relay_client.post("/relay/api/v1/chat/completions", json=payload)
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "distributed_api_v1_relay_disabled"

    assert relay.client_inference_requests == {}
    assert relay.client_responses == {}
    assert relay.streaming_sessions == {}
    assert relay.streaming_sessions_by_client == {}

    diagnostics = relay_client.get("/relay/diagnostics")
    diag_text = _to_text(diagnostics.get_json())
    assert E2EE_SENTINEL_RELAY_STATE not in diag_text


def test_legacy_sink_and_faucet_contract_remains_ciphertext_only(monkeypatch, relay_client):
    monkeypatch.setenv("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", "1")

    relay.known_servers["server-key"] = {
        "public_key": "server-key",
        "last_ping": relay.datetime.now(),
        "last_ping_duration": 10,
    }
    ciphertext_envelope = {
        "chat_history": "abc",
        "cipherkey": "def",
        "iv": "ghi",
        "client_public_key": "client-key",
    }
    relay.client_inference_requests["server-key"] = [ciphertext_envelope]

    sink_response = relay_client.post("/sink", json={"server_public_key": "server-key"})
    assert sink_response.status_code == 200
    sink_payload = sink_response.get_json()
    assert sink_payload["chat_history"] == ciphertext_envelope["chat_history"]
    assert sink_payload["client_public_key"] == ciphertext_envelope["client_public_key"]
    assert "messages" not in _to_text(sink_payload)

    faucet_response = relay_client.post(
        "/faucet",
        json={
            "chat_history": "rsp",
            "cipherkey": "k",
            "iv": "v",
            "server_public_key": "server-key",
            "client_public_key": "client-key",
        },
    )
    assert faucet_response.status_code == 200
    faucet_payload = faucet_response.get_json()
    assert faucet_payload["message"] == "Request received"
    assert E2EE_SENTINEL_RELAY_STATE not in _to_text(relay.client_responses)


def test_local_api_v1_v2_plaintext_remains_allowed_when_not_distributed():
    provider = compute_provider.LocalApiV1ComputeProvider()

    def _fake_response(model, history, **_kwargs):
        return history + [{"role": "assistant", "content": f"{model}-ok"}]

    original = compute_provider.generate_response
    compute_provider.generate_response = _fake_response
    try:
        result = provider.complete_chat(
            model_id="llama-3",
            messages=[{"role": "user", "content": "local-v1-plaintext-ok"}],
            options={"temperature": 0.1},
        )
    finally:
        compute_provider.generate_response = original

    assert result["content"] == "llama-3-ok"
    v2_routes_source = (Path(__file__).resolve().parents[2] / "api" / "v2" / "routes.py").read_text(
        encoding="utf-8"
    )
    assert "distributed_api_v1_relay_disabled" not in v2_routes_source


def test_network_egress_never_leaks_plaintext_sentinel_to_relay(monkeypatch):
    outbound = []
    crypto_manager = compute_provider.CryptoManager()

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return copy.deepcopy(self._payload)

    def _capture(method):
        def _inner(url, **kwargs):
            outbound.append({"method": method, "url": url, "kwargs": copy.deepcopy(kwargs)})
            if str(url).endswith("/api/v1/relay/servers/next"):
                return _Resp(200, {"server_public_key": crypto_manager.public_key_b64})
            if str(url).endswith("/api/v1/relay/requests"):
                return _Resp(500, {"error": "forced faucet failure for test coverage"})
            return _Resp(500, {"error": "unexpected"})

        return _inner

    monkeypatch.setattr(compute_provider.requests, "post", _capture("post"))
    monkeypatch.setattr(compute_provider.requests, "get", _capture("get"))
    monkeypatch.setattr(compute_provider.requests, "request", _capture("request"))
    monkeypatch.setattr(compute_provider.requests.Session, "request", lambda *_a, **_k: _Resp(599, {}))

    provider = compute_provider.DistributedApiV1ComputeProvider(
        base_url="https://relay.example",
        timeout_seconds=0.1,
    )
    with pytest.raises(compute_provider.ComputeProviderError):
        provider.complete_chat(
            model_id="llama-3",
            messages=[{"role": "user", "content": E2EE_SENTINEL_NETWORK}],
            options={"temperature": 0.1},
        )

    request_calls = [call for call in outbound if str(call["url"]).endswith("/api/v1/relay/requests")]
    assert request_calls, "Expected an outbound /api/v1/relay/requests relay request"
    serialized = _to_text(request_calls)
    assert E2EE_SENTINEL_NETWORK not in serialized


def test_logs_and_diagnostics_do_not_echo_plaintext_sentinel(monkeypatch, caplog, relay_client):
    monkeypatch.setenv("TOKENPLACE_ENABLE_LEGACY_RELAY_ROUTES", "1")

    relay.known_servers["server-key"] = {
        "public_key": "server-key",
        "last_ping": relay.datetime.now(),
        "last_ping_duration": 10,
    }
    relay.client_inference_requests["server-key"] = [
        {"api_v1_request": {"messages": [{"role": "user", "content": E2EE_SENTINEL_LOGS}]}}
    ]

    diagnostics_before_sink = _to_text(relay_client.get("/relay/diagnostics").get_json())
    assert E2EE_SENTINEL_LOGS not in diagnostics_before_sink

    relay_logger = logging.getLogger("tokenplace.relay")
    relay_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="tokenplace.relay"):
            sink_response = relay_client.post("/sink", json={"server_public_key": "server-key"})
        assert sink_response.status_code == 200
    finally:
        relay_logger.removeHandler(caplog.handler)

    logs_text = "\n".join(caplog.handler.format(record) for record in caplog.records)
    assert logs_text, "Expected relay logs to be captured during /sink request"
    assert E2EE_SENTINEL_LOGS not in logs_text

    diagnostics_text = _to_text(relay_client.get("/relay/diagnostics").get_json())
    assert E2EE_SENTINEL_LOGS not in diagnostics_text
    assert _to_text(sink_response.get_json()).find(E2EE_SENTINEL_LOGS) == -1
