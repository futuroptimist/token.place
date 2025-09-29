"""Signing utilities for token.place release artifacts."""

from importlib import import_module
from types import ModuleType
from typing import Any

__all__ = [
    "relay_signature",
    "DEFAULT_PUBLIC_KEY_PATH",
    "load_public_key",
    "verify_file_signature",
    "verify_signature_bytes",
]


def _relay_signature_module() -> ModuleType:
    return import_module(f"{__name__}.relay_signature")


def __getattr__(name: str) -> Any:
    if name == "relay_signature":
        return _relay_signature_module()
    if name in {
        "DEFAULT_PUBLIC_KEY_PATH",
        "load_public_key",
        "verify_file_signature",
        "verify_signature_bytes",
    }:
        module = _relay_signature_module()
        return getattr(module, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__() -> list[str]:
    return sorted(__all__)
