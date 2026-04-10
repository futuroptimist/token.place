"""Tests for the legacy ``server.server_app`` compatibility shim."""

import argparse
from unittest.mock import patch
from unittest.mock import MagicMock

import server
import server.server_app as shim


def test_shim_re_exports_canonical_entrypoints():
    assert shim.ServerApp is server.ServerApp
    assert callable(shim.parse_args)
    assert callable(shim.main)


def test_shim_parse_args_matches_canonical_defaults(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["server.py"])
    args = shim.parse_args()
    assert args.server_port == 3000
    assert args.server_host == "127.0.0.1"
    assert args.relay_port is None
    assert args.use_mock_llm is False


def test_shim_main_delegates_to_canonical_server(monkeypatch):
    args = argparse.Namespace(
        server_port=1111,
        server_host="0.0.0.0",
        relay_port=2222,
        relay_url="http://relay.example.com",
        use_mock_llm=False,
    )
    mock_server = MagicMock()

    monkeypatch.setattr(shim, "parse_args", lambda: args)
    with patch("server.server_app.ServerApp", return_value=mock_server) as server_ctor:
        shim.main()

    server_ctor.assert_called_once_with(
        server_port=1111,
        server_host="0.0.0.0",
        relay_port=2222,
        relay_url="http://relay.example.com",
    )
    mock_server.run.assert_called_once()


def test_shim_relay_target_format_helper_is_canonical():
    assert shim._format_relay_target("http://localhost:5000", 5000) == "http://localhost:5000"
