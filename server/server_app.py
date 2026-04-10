"""Compatibility shim for legacy ``server.server_app`` imports.

Canonical compute-node entrypoint: repository-root ``server.py``.
This module stays intentionally thin and forwards legacy patch points into the
canonical implementation so behavior cannot drift.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_canonical_server_module() -> ModuleType:
    repo_root = Path(__file__).resolve().parents[1]
    canonical_path = repo_root / "server.py"
    spec = importlib.util.spec_from_file_location("tokenplace_canonical_server", canonical_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load canonical server module from {canonical_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_canonical = _load_canonical_server_module()
_canonical_parse_args = _canonical.parse_args

# Re-export legacy patch points used by existing tests/callers.
get_model_manager = _canonical.get_model_manager
get_crypto_manager = getattr(_canonical, "get_crypto_manager", None)
RelayClient = _canonical.RelayClient
log_error = _canonical.log_error
collect_resource_usage = _canonical.collect_resource_usage


class ServerApp(_canonical.ServerApp):
    """Compatibility wrapper that keeps canonical behavior and patch hooks in sync."""

    def __init__(self, *args, **kwargs):
        _canonical.get_model_manager = get_model_manager
        if get_crypto_manager is not None:
            _canonical.get_crypto_manager = get_crypto_manager
        _canonical.RelayClient = RelayClient
        _canonical.collect_resource_usage = collect_resource_usage
        super().__init__(*args, **kwargs)


def parse_args():
    return _canonical_parse_args()


def main():
    args = parse_args()

    if getattr(args, "use_mock_llm", False):
        import os

        os.environ["USE_MOCK_LLM"] = "1"

    server = ServerApp(
        server_port=args.server_port,
        server_host=getattr(args, "server_host", "127.0.0.1"),
        relay_port=args.relay_port,
        relay_url=args.relay_url,
    )
    server.run()


config = _canonical.config
argparse = _canonical.argparse
_format_relay_target = _canonical._format_relay_target

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
