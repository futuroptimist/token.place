"""Legacy compatibility shim that delegates to canonical root-level ``server.py``.

Do not add compute-node logic here. Keep this module as a thin import/dispatch layer
so contributors and operators always target ``python server.py`` as the canonical entrypoint.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_ROOT_SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
_SPEC = importlib.util.spec_from_file_location("tokenplace_canonical_server", _ROOT_SERVER_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - defensive import guard
    raise ImportError(f"Unable to load canonical server module at {_ROOT_SERVER_PATH}")

_CANONICAL_SERVER: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_CANONICAL_SERVER)

ServerApp = _CANONICAL_SERVER.ServerApp
parse_args = _CANONICAL_SERVER.parse_args
main = _CANONICAL_SERVER.main
_format_relay_target = _CANONICAL_SERVER._format_relay_target

__all__ = ["ServerApp", "parse_args", "main", "_format_relay_target"]


def __getattr__(name: str):
    """Expose canonical server module attributes for backwards-compatible imports/tests."""

    return getattr(_CANONICAL_SERVER, name)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
