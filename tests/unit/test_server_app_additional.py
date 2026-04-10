"""Additional compatibility checks for ``server.server_app`` shim exports."""

from __future__ import annotations

import server.server_app as shim


def test_shim_getattr_exposes_canonical_helpers():
    assert shim.resolve_relay_url is shim._CANONICAL_SERVER.resolve_relay_url
    assert shim.resolve_relay_port is shim._CANONICAL_SERVER.resolve_relay_port


def test_shim_module_main_is_canonical_main():
    assert shim.main is shim._CANONICAL_SERVER.main
