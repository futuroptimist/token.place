from unittest.mock import MagicMock

from server.server_app import ServerApp


def test_server_app_routes_and_runtime_wiring(monkeypatch):
    runtime = MagicMock()
    runtime.relay_client = MagicMock()
    runtime.ensure_model_ready.return_value = True
    monkeypatch.setattr(
        "server.server_app.ComputeNodeRuntime",
        MagicMock(return_value=runtime),
    )

    server = ServerApp(server_port=9000, relay_port=9001, relay_url="http://localhost")

    assert server.server_port == 9000
    assert server.relay_port == 9001
    assert server.relay_url == "http://localhost"
    assert server.relay_client is runtime.relay_client

    client = server.app.test_client()
    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"
