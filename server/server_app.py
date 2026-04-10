"""Legacy compatibility shim for the canonical compute-node entrypoint.

`server.py` is the only canonical compute-node implementation.  This module is
retained to keep older imports working while delegating all behavior to
`server.py` so compatibility paths cannot drift.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


_CANONICAL_SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
_canonical_server: ModuleType | None = None


def _load_canonical() -> ModuleType:
    """Load and cache the canonical server module on first use."""

    global _canonical_server
    if _canonical_server is not None:
        return _canonical_server

    canonical_spec = importlib.util.spec_from_file_location(
        "tokenplace_canonical_server",
        _CANONICAL_SERVER_PATH,
    )
    if canonical_spec is None or canonical_spec.loader is None:  # pragma: no cover
        raise RuntimeError(
            f"unable to load canonical server module from {_CANONICAL_SERVER_PATH}"
        )

    canonical_module = importlib.util.module_from_spec(canonical_spec)
    canonical_spec.loader.exec_module(canonical_module)
    _canonical_server = canonical_module
    return canonical_module


def __getattr__(name: str):
    if name in {
        "ServerApp",
        "_first_env",
        "_resolve_relay_url",
        "_resolve_relay_port",
        "_format_relay_target",
    }:
        return getattr(_load_canonical(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def parse_args():
    """Compatibility wrapper around canonical CLI parsing."""

    return _load_canonical().parse_args()


def main() -> None:
    """Compatibility wrapper that delegates startup to ``server.py``."""

    _load_canonical().main()

__all__ = [
    "ServerApp",
    "parse_args",
    "main",
    "_first_env",
    "_resolve_relay_url",
    "_resolve_relay_port",
    "_format_relay_target",
]


if __name__ == "__main__":  # pragma: no cover
    main()
