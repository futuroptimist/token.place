"""Compatibility tests for legacy server.server_app shim."""

from __future__ import annotations

from unittest.mock import MagicMock

import server.server_app as sa


def test_server_app_shim_reexports_canonical_symbols():
    canonical = sa._get_canonical()
    assert callable(canonical.ServerApp)
    assert callable(sa.parse_args)
    assert callable(sa.main)


def test_server_app_main_delegates_to_canonical(monkeypatch):
    canonical = sa._get_canonical()
    mock_main = MagicMock()
    monkeypatch.setattr(canonical, "main", mock_main)

    sa.main()

    mock_main.assert_called_once_with()


def test_parse_args_still_available(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["server.py", "--server_port", "3333"])
    args = sa.parse_args()
    assert args.server_port == 3333
