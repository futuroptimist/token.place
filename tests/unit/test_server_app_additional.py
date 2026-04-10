"""Additional drift-guard tests for ``server.server_app`` compatibility shim."""

import server
import server.server_app as shim


def test_shim_server_class_name_matches_canonical():
    assert shim.ServerApp.__name__ == server.ServerApp.__name__


def test_shim_exports_no_shadow_server_app_implementation():
    assert "ServerApp" in shim.__all__
    assert shim.ServerApp is server.ServerApp
