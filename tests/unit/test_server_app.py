"""Tests for the legacy ``server.server_app`` compatibility shim."""

from unittest.mock import patch

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


def test_shim_main_delegates_to_canonical_server():
    with patch("server.server_app._load_canonical") as load_canonical:
        canonical_module = load_canonical.return_value
        shim.main()
    canonical_module.main.assert_called_once_with()


def test_shim_limits_exports_to_compat_entrypoints():
    assert shim.__all__ == ["ServerApp", "parse_args", "main"]
