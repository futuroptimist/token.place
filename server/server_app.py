"""Compatibility shim for legacy ``server.server_app`` imports.

Canonical compute-node entrypoint: repository-root ``server.py``.
This module stays intentionally thin and forwards legacy patch points into the
canonical implementation so behavior cannot drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from utils.crypto.crypto_manager import get_crypto_manager as _default_get_crypto_manager
from utils.llm.model_manager import get_model_manager as _default_get_model_manager
from utils.networking.relay_client import RelayClient as _default_relay_client
from utils.system import collect_resource_usage as _default_collect_resource_usage

_CANONICAL_MODULE_NAME = "tokenplace_canonical_server"
_canonical: ModuleType | None = None


def _load_canonical_server_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[1]
    canonical_path = repo_root / "server.py"
    spec = importlib.util.spec_from_file_location(_CANONICAL_MODULE_NAME, canonical_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load canonical server module from {canonical_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CANONICAL_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def _get_canonical() -> ModuleType:
    global _canonical
    if _canonical is None:
        _canonical = sys.modules.get(_CANONICAL_MODULE_NAME)
    if _canonical is None:
        _canonical = _load_canonical_server_module()
    return _canonical


# Re-export legacy patch points used by existing tests/callers.
get_model_manager = _default_get_model_manager
get_crypto_manager = _default_get_crypto_manager
RelayClient = _default_relay_client
collect_resource_usage = _default_collect_resource_usage


def _sync_patch_points() -> ModuleType:
    canonical = _get_canonical()
    canonical.get_model_manager = get_model_manager
    canonical.get_crypto_manager = get_crypto_manager
    canonical.RelayClient = RelayClient
    canonical.collect_resource_usage = collect_resource_usage
    return canonical


class ServerApp:
    """Compatibility wrapper that returns canonical ServerApp instances lazily."""

    def __new__(cls, *args, **kwargs):
        canonical = _sync_patch_points()
        return canonical.ServerApp(*args, **kwargs)


def parse_args():
    return _get_canonical().parse_args()


def main():
    canonical = _sync_patch_points()
    return canonical.main()


def __getattr__(name: str):
    if name == "config":
        return _get_canonical().config
    if name == "argparse":
        return _get_canonical().argparse
    if name == "_format_relay_target":
        return _get_canonical()._format_relay_target
    if name == "log_error":
        return _get_canonical().log_error
    raise AttributeError(name)


__all__ = [
    "ServerApp",
    "parse_args",
    "main",
    "config",
    "argparse",
    "_format_relay_target",
    "get_model_manager",
    "get_crypto_manager",
    "RelayClient",
    "log_error",
    "collect_resource_usage",
]
