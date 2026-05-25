"""Regression tests for deterministic bridge startup imports without requests."""

from __future__ import annotations

import importlib
import socket
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec


class _RequestsImportBlocker(MetaPathFinder):
    def find_spec(self, fullname: str, _path: object, _target: object = None) -> ModuleSpec | None:
        if fullname == "requests" or fullname.startswith("requests."):
            raise ModuleNotFoundError("blocked for startup import regression test")
        return None


def test_bridge_startup_imports_do_not_depend_on_requests(monkeypatch):
    blocker = _RequestsImportBlocker()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])

    for module_name in (
        "requests",
        "utils.networking.http_requests_compat",
        "utils.networking.relay_client",
        "utils.llm.model_manager",
    ):
        sys.modules.pop(module_name, None)

    relay_client = importlib.import_module("utils.networking.relay_client")
    model_manager = importlib.import_module("utils.llm.model_manager")

    assert hasattr(relay_client, "RelayClient")
    assert hasattr(model_manager, "ModelManager")


def test_requests_compat_exposes_expected_surface_without_requests(monkeypatch):
    blocker = _RequestsImportBlocker()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    sys.modules.pop("utils.networking.http_requests_compat", None)

    module = importlib.import_module("utils.networking.http_requests_compat")

    assert hasattr(module.requests, "get")
    assert hasattr(module.requests, "post")
    assert hasattr(module.requests, "RequestException")
    assert hasattr(module.requests, "ConnectionError")
    assert hasattr(module.requests, "Timeout")


def test_requests_compat_maps_direct_socket_timeout_to_timeout(monkeypatch):
    module = importlib.import_module("utils.networking.http_requests_compat")

    def _raise_timeout(*_args, **_kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(module.urllib_request, "urlopen", _raise_timeout)

    try:
        module.requests.get("https://example.test", timeout=0.01)
        assert False, "expected timeout exception"
    except module.requests.Timeout:
        pass
