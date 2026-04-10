"""Compatibility coverage for ``server.server_app`` delegation behavior."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import server.server_app as shim


def test_server_app_shim_delegates_to_canonical_server_class():
    """Legacy imports should resolve to the canonical root server implementation."""

    assert shim.ServerApp is shim._CANONICAL_SERVER.ServerApp


def test_parse_args_defaults(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["server.py"])
    args = shim.parse_args()
    assert args.server_port == 3000
    assert args.relay_port is None
    assert args.server_host == "127.0.0.1"
    assert args.use_mock_llm is False


def test_main_invocation_delegates_to_canonical_constructor(monkeypatch):
    args = shim._CANONICAL_SERVER.argparse.Namespace(
        server_port=1111,
        server_host="0.0.0.0",
        relay_port=2222,
        relay_url="http://foo",
        use_mock_llm=True,
    )
    monkeypatch.setattr(shim._CANONICAL_SERVER, "parse_args", lambda: args)
    mock_app = MagicMock()
    monkeypatch.setattr(shim._CANONICAL_SERVER, "ServerApp", MagicMock(return_value=mock_app))
    monkeypatch.delenv("USE_MOCK_LLM", raising=False)

    shim.main()

    shim._CANONICAL_SERVER.ServerApp.assert_called_once_with(
        server_port=1111,
        server_host="0.0.0.0",
        relay_port=2222,
        relay_url="http://foo",
    )
    mock_app.run.assert_called_once()
    assert os.environ["USE_MOCK_LLM"] == "1"


def test_format_relay_target_avoids_duplicate_port():
    assert shim._format_relay_target("http://localhost:5000", 5000) == "http://localhost:5000"
