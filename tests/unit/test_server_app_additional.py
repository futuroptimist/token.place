"""Additional drift guards for ``server.server_app`` compatibility behavior."""

from __future__ import annotations

import server.server_app as shim


def test_shim_all_exports_only_compat_surface():
    assert shim.__all__ == ["ServerApp", "main", "parse_args"]


def test_shim_loads_canonical_server_from_repo_root():
    module = shim._load_canonical_module()
    assert module.__file__.endswith("/server.py")


def test_shim_server_app_matches_exported_symbol():
    module = shim._load_canonical_module()
    assert shim.ServerApp is module.ServerApp
