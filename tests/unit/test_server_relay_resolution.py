import importlib.util
from pathlib import Path


def _load_server_script():
    script_path = Path(__file__).resolve().parents[2] / "server.py"
    spec = importlib.util.spec_from_file_location("server_script", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_resolve_relay_url_defaults_to_token_place():
    server = _load_server_script()
    assert server._resolve_relay_url("https://token.place") == "https://token.place"


def test_resolve_relay_port_omits_default_port_for_https():
    server = _load_server_script()
    assert server._resolve_relay_port(None, "https://token.place") is None


def test_resolve_relay_port_uses_explicit_localhost_url_port():
    server = _load_server_script()
    assert server._resolve_relay_port(None, "http://localhost:5000") == 5000


def test_resolve_relay_port_uses_explicit_loopback_url_port():
    server = _load_server_script()
    assert server._resolve_relay_port(None, "http://127.0.0.1:5010") == 5010


def test_resolve_relay_port_respects_cli_port_override_for_https():
    server = _load_server_script()
    assert server._resolve_relay_port(7443, "https://token.place") == 7443
