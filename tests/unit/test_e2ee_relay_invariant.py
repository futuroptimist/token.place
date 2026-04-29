import json
from pathlib import Path

import pytest

import relay
from api.v1 import compute_provider
from relay import app, client_inference_requests, client_responses, streaming_sessions

RELAY_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_REACH_RELAY_PLAINTEXT"
NETWORK_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_LEAVE_PROCESS_AS_PLAINTEXT"
LOG_SENTINEL = "E2EE_SENTINEL_SHOULD_NEVER_APPEAR_IN_LOGS_OR_DIAGNOSTICS"


@pytest.fixture
def relay_client():
    app.config["TESTING"] = True
    relay.known_servers.clear()
    client_inference_requests.clear()
    client_responses.clear()
    streaming_sessions.clear()
    with app.test_client() as client:
        yield client


def _flatten(value):
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return repr(value)


def test_static_forbidden_plaintext_patterns_regression_guard():
    repo_root = Path(__file__).resolve().parents[2]
    targets = [
        repo_root / "relay.py",
        repo_root / "api/v1/compute_provider.py",
        repo_root / "api/v1/routes.py",
        repo_root / "desktop-tauri/src-tauri/python/compute_node_bridge.py",
    ]

    forbidden = [
        "client_inference_requests.setdefault(server_public_key, []).append({\"api_v1_request\"",
        '"api_v1_request": {"messages"',
        '"/relay/api/v1/chat/completions"',
        "api_v1_request.messages",
    ]
    allowed_exceptions = {
        "api/v1/compute_provider.py": ["\"api_v1_request\": {"],
        "relay.py": ["if 'api_v1_request' in request_payload:"],
    }

    for marker in forbidden:
        for path in targets:
            content = path.read_text(encoding="utf-8")
            if marker in content:
                exception_hits = any(
                    marker in allowed for allowed in allowed_exceptions.get(path.relative_to(repo_root).as_posix(), [])
                )
                assert exception_hits, f"Forbidden plaintext relay pattern in {path}: {marker}"


def test_runtime_relay_state_never_contains_plaintext_sentinel(relay_client):
    response = relay_client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": RELAY_SENTINEL}],
            "distributed": True,
        },
    )
    assert response.status_code == 503

    state_dump = _flatten(
        {
            "client_inference_requests": client_inference_requests,
            "client_responses": client_responses,
            "streaming_sessions": streaming_sessions,
            "diagnostics": relay_client.get("/relay/diagnostics").get_json(),
        }
    )
    assert RELAY_SENTINEL not in state_dump
    assert all(not queued for queued in client_inference_requests.values())


def test_network_egress_guard_no_plaintext_sentinel_to_relay(monkeypatch):
    outbound = []

    def _record(method_name):
        def _inner(url, **kwargs):
            outbound.append({"method": method_name, "url": url, "kwargs": kwargs})
            class _Resp:
                status_code = 503

                @staticmethod
                def json():
                    return {"error": {"code": "distributed_api_v1_relay_disabled"}}

            return _Resp()

        return _inner

    monkeypatch.setattr(compute_provider.requests, "post", _record("post"))
    monkeypatch.setattr(compute_provider.requests, "get", _record("get"))
    monkeypatch.setattr(compute_provider.requests, "request", lambda *a, **k: _record("request")(a[1], **k))
    monkeypatch.setattr(compute_provider.requests.Session, "request", lambda *a, **k: _record("session")(a[2], **k))

    provider = compute_provider.DistributedApiV1ComputeProvider(
        base_url="https://relay.example",
        timeout_seconds=0.01,
    )
    with pytest.raises(compute_provider.ComputeProviderError):
        provider.complete_chat(
            model_id="llama-3-8b-instruct",
            messages=[{"role": "user", "content": NETWORK_SENTINEL}],
        )

    for call in outbound:
        assert NETWORK_SENTINEL not in _flatten(call)


def test_log_and_diagnostics_never_echo_plaintext(caplog, relay_client):
    caplog.set_level("INFO")
    response = relay_client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": LOG_SENTINEL}],
            "distributed": True,
        },
    )
    assert response.status_code == 503

    logs_text = "\n".join(record.getMessage() for record in caplog.records)
    assert LOG_SENTINEL not in logs_text
    assert LOG_SENTINEL not in _flatten(response.get_json())
    assert LOG_SENTINEL not in _flatten(relay_client.get("/relay/diagnostics").get_json())


def test_contract_local_plaintext_allowed_distributed_plaintext_forbidden(relay_client):
    local_response = relay_client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "local-plaintext-ok"}],
            "distributed": False,
        },
    )
    assert local_response.status_code in {200, 500, 503}

    distributed_response = relay_client.post(
        "/api/v1/chat/completions",
        json={
            "model": "llama-3-8b-instruct",
            "messages": [{"role": "user", "content": "distributed-plaintext-forbidden"}],
            "distributed": True,
        },
    )
    assert distributed_response.status_code == 503
    payload = distributed_response.get_json()
    assert payload and payload.get("error", {}).get("code") == "distributed_api_v1_relay_disabled"


def test_contract_legacy_sink_faucet_ciphertext_only(relay_client):
    sink_registration = relay_client.post("/sink", json={"server_public_key": "server-key"})
    assert sink_registration.status_code == 200

    faucet_payload = {
        "client_public_key": "client-key",
        "server_public_key": "server-key",
        "chat_history": "ciphertext",
        "cipherkey": "encrypted-key",
        "iv": "encrypted-iv",
    }
    faucet_response = relay_client.post("/faucet", json=faucet_payload)
    assert faucet_response.status_code == 200

    work = relay_client.post("/sink", json={"server_public_key": "server-key", "max_batch_size": 1}).get_json()
    assert work["chat_history"] == "ciphertext"
    assert "messages" not in _flatten(work)
