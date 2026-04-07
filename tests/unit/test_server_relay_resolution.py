"""Focused tests for server relay URL and port resolution defaults."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SERVER_ENTRYPOINT_PATH = Path(__file__).resolve().parents[2] / "server.py"
_spec = spec_from_file_location("server_entrypoint", SERVER_ENTRYPOINT_PATH)
assert _spec and _spec.loader
server_entrypoint = module_from_spec(_spec)
_spec.loader.exec_module(server_entrypoint)


def test_resolve_relay_url_defaults_to_token_place_when_no_env(monkeypatch):
    monkeypatch.delenv("TOKENPLACE_RELAY_URL", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("TOKENPLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_RELAY_UPSTREAM_URL", raising=False)
    monkeypatch.delenv("RELAY_URL", raising=False)

    assert server_entrypoint._resolve_relay_url("https://token.place") == "https://token.place"


def test_resolve_relay_port_uses_port_from_explicit_local_override() -> None:
    assert server_entrypoint._resolve_relay_port(None, "http://127.0.0.1:5010") == 5010
    assert server_entrypoint._resolve_relay_port(None, "http://localhost:5000") == 5000


def test_resolve_relay_port_keeps_https_target_without_explicit_port() -> None:
    assert server_entrypoint._resolve_relay_port(None, "https://token.place") is None
