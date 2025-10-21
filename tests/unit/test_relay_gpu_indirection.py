import pytest

import relay


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear relay upstream env vars before each test."""

    for key in (
        "TOKENPLACE_GPU_HOST",
        "TOKENPLACE_GPU_PORT",
        "TOKENPLACE_RELAY_UPSTREAM_URL",
        "GPU_SERVER_HOST",
        "GPU_SERVER_PORT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_load_upstream_config_prefers_explicit_gpu_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENPLACE_GPU_HOST", "gpu.example.com")
    monkeypatch.setenv("TOKENPLACE_GPU_PORT", "5015")
    monkeypatch.setenv("TOKENPLACE_RELAY_UPSTREAM_URL", "http://gpu.example.com:5015")

    config = relay._load_upstream_config()

    assert config["gpu_host"] == "gpu.example.com"
    assert config["gpu_port"] == 5015
    assert config["upstream_url"] == "http://gpu.example.com:5015"


def test_load_upstream_config_derives_port_from_upstream_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENPLACE_RELAY_UPSTREAM_URL", "http://gpu-server:5015")

    config = relay._load_upstream_config()

    assert config["gpu_host"] == "gpu-server"
    assert config["gpu_port"] == 5015
    assert config["upstream_url"] == "http://gpu-server:5015"
