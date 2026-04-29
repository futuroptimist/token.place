import copy
import json
from pathlib import Path

import pytest

import relay
from api.v1 import compute_provider

E2EE_SENTINEL_RELAY_STATE = "E2EE_SENTINEL_SHOULD_NEVER_REACH_RELAY_PLAINTEXT"
E2EE_SENTINEL_NETWORK = "E2EE_SENTINEL_SHOULD_NEVER_LEAVE_PROCESS_AS_PLAINTEXT"
E2EE_SENTINEL_LOGS = "E2EE_SENTINEL_SHOULD_NEVER_APPEAR_IN_LOGS_OR_DIAGNOSTICS"


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
        "client_inference_requests.setdefault(server_public_key, []).append({\"api_v1_request\"",
        "client_inference_requests.setdefault(server_public_key, []).append({'api_v1_request'",
        "\"api_v1_request\": {\"messages\"",
        "'api_v1_request': {'messages'",
        "requests.post(self._relay_url('/relay/api/v1/chat/completions')",
        "requests.post(self._relay_url(\"/relay/api/v1/chat/completions\")",
    ]

    scanned = "\n\n".join(f.read_text(encoding="utf-8") for f in targets if f.exists())

    matched = [pattern for pattern in forbidden_patterns if pattern in scanned]
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


def test_network_egress_never_leaks_plaintext_sentinel_to_relay(monkeypatch):
    outbound = []

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return copy.deepcopy(self._payload)

    def _capture(method):
        def _inner(url, **kwargs):
            outbound.append({"method": method, "url": url, "kwargs": copy.deepcopy(kwargs)})
            if str(url).endswith("/next_server"):
                return _Resp(200, {"error": {"code": 503, "message": "No servers available"}})
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

    relay_calls = [call for call in outbound if "relay" in call["url"]]
    assert relay_calls, "Expected at least one relay-targeted outbound call"
    serialized = _to_text(relay_calls)
    assert E2EE_SENTINEL_NETWORK not in serialized


def test_logs_and_diagnostics_do_not_echo_plaintext_sentinel(caplog, relay_client):
    relay.known_servers["server-key"] = {
        "public_key": "server-key",
        "last_ping": relay.datetime.now(),
        "last_ping_duration": 10,
    }
    relay.client_inference_requests["server-key"] = [
        {"api_v1_request": {"messages": [{"role": "user", "content": E2EE_SENTINEL_LOGS}]}}
    ]

    sink_response = relay_client.post("/sink", json={"server_public_key": "server-key"})
    assert sink_response.status_code == 200

    logs_text = "\n".join(record.getMessage() for record in caplog.records)
    assert E2EE_SENTINEL_LOGS not in logs_text

    diagnostics_text = _to_text(relay_client.get("/relay/diagnostics").get_json())
    assert E2EE_SENTINEL_LOGS not in diagnostics_text
    assert _to_text(sink_response.get_json()).find(E2EE_SENTINEL_LOGS) == -1
