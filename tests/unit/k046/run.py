"""Run K046 synthetic tests with guards installed before application imports.

Usage: python tests/unit/k046/run.py
No network, process launch, model load, real crypto or dependency installation.
"""

import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
os.environ["TOKEN_PLACE_ENV"] = "testing"


def forbidden(*_args, **_kwargs):
    raise AssertionError("K046 offline guard: forbidden I/O or real execution")


def audit(event, args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.fork", "os.posix_spawn"}:
        forbidden()


sys.addaudithook(audit)
# Imports below cannot use sockets, DNS or launch processes, including at collection.
import pytest  # noqa: E402
import requests  # noqa: E402
import encrypt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

with (
    patch.object(encrypt, "generate_keys", return_value=(b"synthetic-private", b"synthetic-public")),
    patch.object(encrypt, "encrypt", forbidden),
    patch.object(encrypt, "decrypt", forbidden),
    patch.object(rsa, "generate_private_key", forbidden),
    patch.object(requests.sessions.Session, "send", forbidden),
):
    from api.v1 import models
    from utils.crypto.crypto_manager import CryptoManager

    with (
        patch.object(models, "get_model_instance", forbidden),
        patch.object(models, "generate_response", forbidden),
        patch.object(CryptoManager, "__init__", forbidden),
    ):
        raise SystemExit(pytest.main([
            "-q", "-o", "addopts=", "-o", "log_cli=false", "--confcutdir=tests/unit/k046",
            "tests/unit/k046/test_routing.py", *sys.argv[1:],
        ]))
