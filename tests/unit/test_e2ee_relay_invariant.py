import json
from pathlib import Path

import pytest

import relay as relay_module
from api.v1 import compute_provider
from relay import app, client_inference_requests, client_responses, known_servers, streaming_sessions

RELAY_STATE_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_REACH_RELAY_PLAINTEXT"
NETWORK_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_LEAVE_PROCESS_AS_PLAINTEXT"
LOG_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_APPEAR_IN_LOGS_OR_DIAGNOSTICS"


def _scan_text(value):
    try:
        return json.dumps(value, sort_keys=True, default=repr)
    except Exception:
        return repr(value)


@pytest.fixture
def relay_client():
    app.config["TESTING"] = True
    known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()
    streaming_sessions.clear()
    with app.test_client() as client:
        yield client


def test_static_forbidden_plaintext_patterns_absent():
    files = [Path("relay.py"), Path("api/v1/compute_provider.py"), Path("server.py")]
    disallowed = [
        "client_inference_requests.setdefault(server_public_key, []).append({'api_v1_request'",
        '/relay/api/v1/chat/completions", json={"messages"',
        "api_v1_request.messages",
    ]
    for file in files:
        content = file.read_text(encoding="utf-8")
        for token in disallowed:
            assert token not in content, f"{token} unexpectedly present in {file}"


def test_runtime_relay_state_sentinel_not_queued_when_distributed_plaintext_path_called(relay_client):
    response = relay_client.post(
        "/relay/api/v1/chat/completions",
        json={"messages": [{"role": "user", "content": RELAY_STATE_SENTINEL}]},
    )
    assert response.status_code == 503
    payload = response.get_json()
    assert payload["error"]["code"] == "distributed_api_v1_relay_disabled"
    assert RELAY_STATE_SENTINEL not in _scan_text(client_inference_requests)
    assert RELAY_STATE_SENTINEL not in _scan_text(client_responses)
    assert RELAY_STATE_SENTINEL not in _scan_text(streaming_sessions)


def test_network_egress_plaintext_sentinel_never_leaves_process(monkeypatch):
    captured = []

    def _capture(method, url, **kwargs):
        captured.append({"method": method, "url": url, "kwargs": kwargs})
        raise AssertionError("network should not be called in fail-closed mode")

    monkeypatch.setattr(compute_provider.requests, "post", lambda url, **kwargs: _capture("post", url, **kwargs))
    monkeypatch.setattr(compute_provider.requests, "get", lambda url, **kwargs: _capture("get", url, **kwargs))
    monkeypatch.setattr(compute_provider.requests, "request", lambda method, url, **kwargs: _capture(method, url, **kwargs))
    monkeypatch.setattr(
        compute_provider.requests.sessions.Session,
        "request",
        lambda self, method, url, **kwargs: _capture(method, url, **kwargs),
    )

    with app.test_client() as client:
        resp = client.post(
            "/relay/api/v1/chat/completions",
            json={"messages": [{"role": "user", "content": NETWORK_SENTINEL}]},
        )
    assert resp.status_code == 503
    for outbound in captured:
        assert NETWORK_SENTINEL not in _scan_text(outbound)


def test_logs_and_diagnostics_never_echo_plaintext_sentinel(relay_client, caplog):
    caplog.set_level("INFO")
    relay_client.post(
        "/relay/api/v1/chat/completions",
        json={"messages": [{"role": "user", "content": LOG_SENTINEL}]},
    )
    diagnostics = relay_client.get("/relay/diagnostics").get_json()
    assert LOG_SENTINEL not in diagnostics.__repr__()
    for record in caplog.records:
        assert LOG_SENTINEL not in record.getMessage()
        assert LOG_SENTINEL not in repr(record.__dict__)


def test_local_plaintext_allowed_but_relay_plaintext_forbidden(monkeypatch):
    provider = compute_provider.LocalApiV1ComputeProvider()
    monkeypatch.setattr(
        compute_provider,
        "generate_response",
        lambda _model, messages, **_opts: [*messages, {"role": "assistant", "content": "ok"}],
    )
    response = provider.complete_chat(
        model_id="model",
        messages=[{"role": "user", "content": "local plaintext is allowed"}],
    )
    assert response["content"] == "ok"

    with app.test_client() as client:
        relay_resp = client.post(
            "/relay/api/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "forbidden distributed plaintext"}]},
        )
    assert relay_resp.status_code == 503


def test_sink_and_faucet_ciphertext_contract_preserved(relay_client):
    server_key = "server-key"
    relay_client.post("/sink", json={"server_public_key": server_key})
    faucet_payload = {
        "client_public_key": "client-key",
        "server_public_key": server_key,
        "chat_history": "ciphertext",
        "cipherkey": "cipherkey",
        "iv": "iv",
    }
    faucet_response = relay_client.post("/faucet", json=faucet_payload)
    assert faucet_response.status_code == 200
    sink_response = relay_client.post("/sink", json={"server_public_key": server_key}).get_json()
    assert sink_response["chat_history"] == "ciphertext"
