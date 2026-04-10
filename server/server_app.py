"""Legacy compatibility shim for the canonical compute-node entrypoint.

`server.py` is the only canonical compute-node implementation.  This module is
retained to keep older imports working while delegating all behavior to
`server.py` so compatibility paths cannot drift.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


_CANONICAL_SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
_CANONICAL_SERVER_SPEC = importlib.util.spec_from_file_location(
    "tokenplace_canonical_server",
    _CANONICAL_SERVER_PATH,
)
if _CANONICAL_SERVER_SPEC is None or _CANONICAL_SERVER_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f"unable to load canonical server module from {_CANONICAL_SERVER_PATH}")

_canonical_server = importlib.util.module_from_spec(_CANONICAL_SERVER_SPEC)
_CANONICAL_SERVER_SPEC.loader.exec_module(_canonical_server)

ServerApp = _canonical_server.ServerApp
_first_env = _canonical_server._first_env
_resolve_relay_url = _canonical_server._resolve_relay_url
_resolve_relay_port = _canonical_server._resolve_relay_port
_format_relay_target = _canonical_server._format_relay_target


def parse_args():
    """Compatibility wrapper around canonical CLI parsing."""

    return _canonical_server.parse_args()


def main() -> None:
    """Compatibility wrapper that delegates startup to ``server.py``."""

    args = parse_args()
    if args.use_mock_llm:
        os.environ["USE_MOCK_LLM"] = "1"
    relay_url = _resolve_relay_url(args.relay_url)
    relay_port = _resolve_relay_port(args.relay_port, relay_url)
    server = ServerApp(
        server_port=args.server_port,
        server_host=args.server_host,
        relay_port=relay_port,
        relay_url=relay_url,
    )
    server.run()

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
