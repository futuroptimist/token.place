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


def test_server_app_uses_legacy_patch_hooks_for_runtime(monkeypatch):
    canonical = sa._get_canonical()
    model_manager = MagicMock(use_mock_llm=True)
    crypto_manager = MagicMock()
    relay_client = MagicMock()
    relay_ctor = MagicMock(return_value=relay_client)

    monkeypatch.setattr(canonical.ServerApp, "initialize_llm", lambda self: None)
    monkeypatch.setattr(sa, "get_model_manager", lambda: model_manager)
    monkeypatch.setattr(sa, "get_crypto_manager", lambda: crypto_manager)
    monkeypatch.setattr(sa, "RelayClient", relay_ctor)

    app = sa.ServerApp(server_port=3333, relay_url="http://localhost", relay_port=5555)

    assert app.runtime.model_manager is model_manager
    assert app.runtime.crypto_manager is crypto_manager
    assert app.runtime.relay_client is relay_client
    relay_ctor.assert_called_once_with(
        base_url="http://localhost",
        port=5555,
        crypto_manager=crypto_manager,
        model_manager=model_manager,
    )
