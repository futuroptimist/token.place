from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


MODULE_PATH = Path("desktop-tauri/src-tauri/python/compute_node_bridge.py")
spec = importlib.util.spec_from_file_location("compute_node_bridge", MODULE_PATH)
compute_node_bridge = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(compute_node_bridge)


class FakeModelManager:
    def __init__(self) -> None:
        self.model_path = ""
        self.default_n_gpu_layers = -1


class FakeRuntime:
    def __init__(self, *_args, **_kwargs) -> None:
        self.model_manager = FakeModelManager()
        self.relay_client = type("RelayClient", (), {"_streaming_enabled": False})()
        self.ensure_calls = 0
        self.processed_payload = None

    def ensure_model_ready(self) -> bool:
        self.ensure_calls += 1
        return True

    def register_and_poll_once(self):
        return {
            "next_ping_in_x_seconds": 0,
            "client_public_key": "abc",
            "chat_history": "cipher",
            "cipherkey": "key",
            "iv": "iv",
            "stream": True,
            "stream_session_id": "session-1",
        }

    def process_relay_request(self, payload):
        self.processed_payload = payload
        return True

    def stop(self) -> None:
        return None


def test_compute_node_bridge_processes_sink_payload(monkeypatch):
    fake_runtime = FakeRuntime()
    emitted = []
    stop_counter = {"n": 0}

    def fake_stop_requested() -> bool:
        stop_counter["n"] += 1
        return stop_counter["n"] > 1

    monkeypatch.setattr(compute_node_bridge, "ComputeNodeRuntime", lambda *_args, **_kwargs: fake_runtime)
    monkeypatch.setattr(compute_node_bridge, "_emit", lambda payload: emitted.append(payload))
    monkeypatch.setattr(compute_node_bridge, "_stop_requested", fake_stop_requested)
    monkeypatch.setattr(compute_node_bridge.time, "sleep", lambda _secs: None)

    args = argparse.Namespace(
        relay_url="https://token.place",
        relay_port=None,
        model_path="/tmp/model.gguf",
        mode="cpu",
        streaming=True,
    )

    rc = compute_node_bridge.run(args)

    assert rc == 0
    assert fake_runtime.ensure_calls == 1
    assert fake_runtime.processed_payload is not None
    assert any(item.get("type") == "status" and item.get("registered") for item in emitted)
    assert emitted[-1] == {"type": "stopped"}


def test_configure_env_for_runtime_resolves_values(monkeypatch):
    monkeypatch.setattr(compute_node_bridge, "resolve_relay_url", lambda relay_url: relay_url)
    monkeypatch.setattr(
        compute_node_bridge,
        "resolve_relay_port",
        lambda relay_port, _relay_url: relay_port,
    )

    args = argparse.Namespace(relay_url="https://relay.example", relay_port=443, streaming=True)
    relay_url, relay_port = compute_node_bridge._configure_env_for_runtime(args)

    assert relay_url == "https://relay.example"
    assert relay_port == 443
