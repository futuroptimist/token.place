"""Compatibility tests for legacy server.server_app shim."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import server.server_app as sa


def test_server_app_shim_reexports_canonical_symbols():
    canonical = sa._canonical
    assert issubclass(sa.ServerApp, canonical.ServerApp)
    assert callable(sa.parse_args)
    assert callable(sa.main)


def test_server_app_main_delegates_to_canonical(monkeypatch):
    args = sa.argparse.Namespace(
        server_port=1111,
        server_host="127.0.0.1",
        relay_port=2222,
        relay_url="http://foo",
        use_mock_llm=True,
    )
    monkeypatch.setattr(sa, "parse_args", lambda: args)

    mock_app = MagicMock()
    monkeypatch.setattr(sa, "ServerApp", MagicMock(return_value=mock_app))
    monkeypatch.delenv("USE_MOCK_LLM", raising=False)

    sa.main()

    sa.ServerApp.assert_called_once_with(
        server_port=1111,
        server_host="127.0.0.1",
        relay_port=2222,
        relay_url="http://foo",
    )
    mock_app.run.assert_called_once()


def test_parse_args_still_available(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["server.py", "--server_port", "3333"])
    args = sa.parse_args()
    assert args.server_port == 3333
