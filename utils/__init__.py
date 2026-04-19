"""Utilities package for token.place.

Avoid importing heavyweight runtime dependencies at module import time. Desktop
sidecars import ``utils.compute_node_runtime`` during early startup, and eager
imports here can trigger unrelated dependency failures before sidecar bootstrap
runs.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["get_model_manager", "get_crypto_manager", "get_temp_dir", "RelayClient"]

_ATTR_TO_MODULE = {
    "get_temp_dir": ("utils.path_handling", "get_temp_dir"),
    "get_model_manager": ("utils.llm.model_manager", "get_model_manager"),
    "get_crypto_manager": ("utils.crypto.crypto_manager", "get_crypto_manager"),
    "RelayClient": ("utils.networking.relay_client", "RelayClient"),
}


def __getattr__(name: str) -> Any:
    module_attr = _ATTR_TO_MODULE.get(name)
    if module_attr is None:
        raise AttributeError(f"module 'utils' has no attribute {name!r}")

    module_name, attr_name = module_attr
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
