import json
from pathlib import Path

import pytest
import requests

import relay as relay_module
from relay import app, client_inference_requests, client_responses, streaming_sessions, streaming_sessions_by_client

RELAY_STATE_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_REACH_RELAY_PLAINTEXT"
NETWORK_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_LEAVE_PROCESS_AS_PLAINTEXT"
LOG_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_APPEAR_IN_LOGS_OR_DIAGNOSTICS"


def _flatten(value):
    if isinstance(value, dict):
        return " ".join(f"{k} {_flatten(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten(v) for v in value)
    return repr(value)


@pytest.fixture
def relay_client():
    app.config["TESTING"] = True
    relay_module.known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()
    streaming_sessions.clear()
    streaming_sessions_by_client.clear()
    with app.test_client() as client:
        yield client


def test_static_forbidden_plaintext_patterns_absent():
    forbidden_regexes = [
        ("relay.py", r"client_inference_requests\.setdefault\([^\n]*\)\.append\(\{[^}]*api_v1_request[^}]*messages"),
        ("api/v1/compute_provider.py", r"requests\.(post|request)\([^\n]*relay/api/v1/chat/completions[^\n]*messages"),
    ]
    for relpath, pattern in forbidden_regexes:
        text = Path(relpath).read_text(encoding="utf-8")
        if __import__("re").search(pattern, text, __import__("re").DOTALL):
            pytest.fail(f"forbidden plaintext relay pattern found in {relpath}: {pattern}")


def test_runtime_relay_state_and_logs_do_not_include_plaintext_sentinel(relay_client, caplog):
    caplog.set_level("INFO")
    response = relay_client.post(
        "/relay/api/v1/chat/completions",
        json={"messages": [{"role": "user", "content": RELAY_STATE_SENTINEL}]},
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["error"]["code"] == "distributed_api_v1_relay_disabled"
    state_blob = _flatten(
        {
            "client_inference_requests": client_inference_requests,
            "client_responses": client_responses,
            "streaming_sessions": streaming_sessions,
            "streaming_sessions_by_client": streaming_sessions_by_client,
            "diagnostics": relay_client.get("/relay/diagnostics").get_json(),
        }
    )
    assert RELAY_STATE_SENTINEL not in state_blob
    assert RELAY_STATE_SENTINEL not in response.get_data(as_text=True)
    assert RELAY_STATE_SENTINEL not in " ".join(r.getMessage() for r in caplog.records)


def test_network_egress_never_sends_plaintext_sentinel(monkeypatch, relay_client):
    calls = []

    def _capture(method):
        def _inner(*args, **kwargs):
            calls.append({"method": method, "args": args, "kwargs": kwargs})
            raise AssertionError("network should not be used by fail-closed relay endpoint")

        return _inner

    monkeypatch.setattr(requests, "post", _capture("post"))
    monkeypatch.setattr(requests, "get", _capture("get"))
    monkeypatch.setattr(requests, "request", _capture("request"))
    monkeypatch.setattr(requests.sessions.Session, "request", _capture("session.request"))

    response = relay_client.post(
        "/relay/api/v1/chat/completions",
        json={"messages": [{"role": "user", "content": NETWORK_SENTINEL}]},
    )
    assert response.status_code == 503
    for call in calls:
        assert NETWORK_SENTINEL not in _flatten(call)


def test_contract_local_plaintext_allowed_but_relay_plaintext_forbidden(monkeypatch):
    from api.v1 import routes

    app.config["TESTING"] = True
    with app.test_client() as client:
        monkeypatch.setattr(
            routes,
            "generate_response",
            lambda _model, messages, **_opts: messages + [{"role": "assistant", "content": "ok"}],
        )
        local = client.post(
            "/api/v1/chat/completions",
            json={"model": "llama-3-8b-instruct", "messages": [{"role": "user", "content": "local plaintext ok"}]},
        )
        assert local.status_code == 200

        relay_resp = client.post(
            "/relay/api/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "forbidden distributed plaintext"}]},
        )
        assert relay_resp.status_code == 503


def test_legacy_faucet_sink_ciphertext_contract(relay_client):
    server_key = "server-key"
    relay_client.post("/sink", json={"server_public_key": server_key})
    payload = {
        "client_public_key": "client-key",
        "server_public_key": server_key,
        "chat_history": "ciphertext",
        "cipherkey": "cipherkey",
        "iv": "iv",
    }
    posted = relay_client.post("/faucet", json=payload)
    assert posted.status_code == 200
    sink = relay_client.post("/sink", json={"server_public_key": server_key}).get_json()
    assert sink["chat_history"] == "ciphertext"
    assert "messages" not in json.dumps(sink)
