"""Regression tests for deterministic bridge startup imports without requests."""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
from pathlib import Path


def test_bridge_startup_imports_do_not_depend_on_requests():
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    script = """
import importlib
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec

class _RequestsImportBlocker(MetaPathFinder):
    def find_spec(self, fullname: str, _path: object, _target: object = None) -> ModuleSpec | None:
        if fullname == "requests" or fullname.startswith("requests."):
            raise ModuleNotFoundError("blocked for startup import regression test")
        return None

sys.meta_path = [_RequestsImportBlocker(), *sys.meta_path]

for module_name in (
    "requests",
    "utils.networking.http_requests_compat",
    "utils.networking.relay_client",
    "utils.llm.model_manager",
):
    sys.modules.pop(module_name, None)

relay_client = importlib.import_module("utils.networking.relay_client")
model_manager = importlib.import_module("utils.llm.model_manager")

if not hasattr(relay_client, "RelayClient"):
    raise AssertionError("RelayClient missing")
if not hasattr(model_manager, "ModelManager"):
    raise AssertionError("ModelManager missing")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(repo_root),
    )

    assert result.returncode == 0, (
        "startup import regression subprocess failed\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_requests_compat_exposes_expected_surface_without_requests(monkeypatch):
    from importlib.abc import MetaPathFinder

    class _RequestsImportBlocker(MetaPathFinder):
        def find_spec(self, fullname: str, _path: object, _target: object = None):
            if fullname == "requests" or fullname.startswith("requests."):
                raise ModuleNotFoundError("blocked for startup import regression test")
            return None

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


def test_model_download_timeout_returns_false(monkeypatch, tmp_path: Path):
    model_manager_module = importlib.import_module("utils.llm.model_manager")

    class _Config:
        is_production = False

        def get(self, key, default=None):
            values = {
                "paths.models_dir": str(tmp_path),
                "model.filename": "model.gguf",
                "model.url": "https://example.test/model.gguf",
                "model.download_chunk_size_mb": 1,
                "model.download_timeout": 1,
            }
            return values.get(key, default)

    def _raise_timeout(*_args, **_kwargs):
        raise model_manager_module.requests.Timeout("timed out")

    monkeypatch.setattr(model_manager_module.requests, "get", _raise_timeout)
    manager = model_manager_module.ModelManager(config=_Config())

    assert manager.download_file_in_chunks(str(tmp_path / "model.gguf"), manager.url, 1) is False
