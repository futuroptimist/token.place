"""Legacy compatibility shim for the canonical ``server.py`` compute-node entrypoint.

This module intentionally delegates to root-level ``server.py`` so that older imports
(``server.server_app``) continue to work while the repository converges on a single
canonical compute-node implementation.
"""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Optional

_CANONICAL_MODULE: Optional[ModuleType] = None


def _load_canonical_module() -> ModuleType:
    """Load and cache the root ``server.py`` module."""

    global _CANONICAL_MODULE
    if _CANONICAL_MODULE is None:
        module_path = Path(__file__).resolve().parents[1] / "server.py"
        spec = spec_from_file_location("tokenplace_canonical_server", module_path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive import guard
            raise RuntimeError("unable to load canonical server.py module")

        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        _CANONICAL_MODULE = module

    return _CANONICAL_MODULE


def __getattr__(name: str):
    """Delegate unknown attributes directly to canonical ``server.py``."""

    return getattr(_load_canonical_module(), name)


def parse_args():
    """Compatibility wrapper around ``server.py`` CLI parsing."""

    return _load_canonical_module().parse_args()


def main():
    """Compatibility wrapper around ``server.py`` main entrypoint."""

    return _load_canonical_module().main()


ServerApp = _load_canonical_module().ServerApp

__all__ = ["ServerApp", "main", "parse_args"]
