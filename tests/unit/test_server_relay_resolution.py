import sys

import server as root_server


def test_parse_args_default_relay_url_is_token_place(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["server.py"])
    args = root_server.parse_args()
    assert args.relay_url == "https://token.place"


def test_resolve_relay_url_prefers_env_override(monkeypatch):
    monkeypatch.setenv("TOKENPLACE_RELAY_URL", "http://127.0.0.1:5010")
    assert root_server._resolve_relay_url("https://token.place") == "http://127.0.0.1:5010"


def test_resolve_relay_port_uses_none_for_https_without_explicit_port():
    assert root_server._resolve_relay_port(5000, "https://token.place") is None


def test_resolve_relay_port_uses_explicit_url_port():
    assert root_server._resolve_relay_port(5000, "http://127.0.0.1:5010") == 5010


def test_resolve_relay_port_keeps_default_for_local_url_without_port():
    assert root_server._resolve_relay_port(5000, "http://localhost") == 5000
