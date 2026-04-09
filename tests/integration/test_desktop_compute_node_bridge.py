import argparse

from desktop_tauri_bridge_loader import load_compute_node_bridge


def test_compute_node_bridge_processes_sink_payload(monkeypatch):
    bridge = load_compute_node_bridge()

    status_events = []

    class FakeRelayClient:
        relay_url = "https://relay.example"

    class FakeRuntime:
        def __init__(self, *_args, **_kwargs):
            self.relay_client = FakeRelayClient()
            self.model_manager = type("ModelManager", (), {"model_path": ""})()
            self._responses = [
                {
                    "next_ping_in_x_seconds": 0,
                    "client_public_key": "client",
                    "chat_history": "ct",
                    "cipherkey": "key",
                    "iv": "iv",
                }
            ]
            self.processed = 0
            self.stopped = 0

        def ensure_model_ready(self):
            return True

        def register_and_poll_once(self):
            if self._responses:
                return self._responses.pop(0)
            return {"next_ping_in_x_seconds": 0}

        def process_relay_request(self, _payload):
            self.processed += 1
            return True

        def stop(self):
            self.stopped += 1

    stop_sequence = iter([False, True])

    monkeypatch.setattr(bridge, "ComputeNodeRuntime", FakeRuntime)
    monkeypatch.setattr(bridge, "resolve_relay_url", lambda url: url)
    monkeypatch.setattr(bridge, "resolve_relay_port", lambda port, _url: port)
    monkeypatch.setattr(bridge, "format_relay_target", lambda url, _port: url)
    monkeypatch.setattr(bridge, "emit_status", lambda **payload: status_events.append(payload))
    monkeypatch.setattr(bridge, "read_stdin_stop_signal", lambda timeout_seconds=0.1: next(stop_sequence))

    args = argparse.Namespace(model="/tmp/model.gguf", relay_url="https://relay.example", relay_port=None, mode="cpu")
    code = bridge.run(args)

    assert code == 0
    assert any(event["state"] == "running" for event in status_events)
    assert any(event["registered"] is True for event in status_events)


def test_compute_node_bridge_reports_model_init_failure(monkeypatch):
    bridge = load_compute_node_bridge()

    status_events = []

    class FailingRuntime:
        def __init__(self, *_args, **_kwargs):
            self.relay_client = type("RelayClient", (), {"relay_url": "https://relay.example"})()
            self.model_manager = type("ModelManager", (), {"model_path": ""})()

        def ensure_model_ready(self):
            return False

        def stop(self):
            return None

    monkeypatch.setattr(bridge, "ComputeNodeRuntime", FailingRuntime)
    monkeypatch.setattr(bridge, "resolve_relay_url", lambda url: url)
    monkeypatch.setattr(bridge, "resolve_relay_port", lambda port, _url: port)
    monkeypatch.setattr(bridge, "format_relay_target", lambda url, _port: url)
    monkeypatch.setattr(bridge, "emit_status", lambda **payload: status_events.append(payload))

    args = argparse.Namespace(model="/tmp/model.gguf", relay_url="https://relay.example", relay_port=None, mode="cpu")
    code = bridge.run(args)

    assert code == 1
    assert status_events[-1]["state"] == "failed"
