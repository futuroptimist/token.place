"""Isolated K046 fixtures; run with --confcutdir=tests/unit/k046."""

import base64
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import Flask


@pytest.fixture
def routing(monkeypatch):
    from api.v1 import compute_provider as cp, routes

    for name in tuple(os.environ):
        if name.startswith(("TOKENPLACE_API_V1_", "TOKENPLACE_DISTRIBUTED_")) or name in (
            *cp._RELAY_INTERNAL_URL_ENVS, *cp._RELAY_PUBLIC_URL_ENVS,
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOKEN_PLACE_ENV", "testing")
    monkeypatch.setattr(cp, "_config_value", lambda _key: "")
    clear_cache = cp._build_api_v1_compute_provider.cache_clear
    clear_cache()
    local = Mock(return_value=[{"role": "assistant", "content": "synthetic local result"}])
    monkeypatch.setattr(cp, "generate_response", local)
    monkeypatch.setattr(routes, "get_models_info", lambda: [{"id": "qwen3-8b-instruct"}])
    monkeypatch.setattr(routes.encryption_manager, "decrypt_message", Mock(return_value=json.dumps(
        [{"role": "user", "content": "synthetic prompt"}]
    ).encode()))
    monkeypatch.setattr(routes.encryption_manager, "encrypt_message", Mock(return_value={"ciphertext": "opaque"}))
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.add_url_rule("/chat", view_func=lambda: routes._handle_chat_completion_request(
        __import__("flask").request.get_json()
    ), methods=["POST"])
    yield SimpleNamespace(cp=cp, routes=routes, client=app.test_client(), local=local)
    clear_cache()


@pytest.fixture
def payload():
    def build(encrypted=True, markers="both"):
        result = {"model": "qwen3-8b-instruct", "messages": [{"role": "user", "content": "synthetic prompt"}]}
        if encrypted:
            result.update(encrypted=True, client_public_key="opaque-client",
                          messages={key: base64.b64encode(b"opaque").decode()
                                    for key in ("ciphertext", "cipherkey", "iv")})
        metadata = {}
        if markers in {"both", "target"}:
            metadata["inference_target"] = "desktop_bridge_api_v1_e2ee"
        if markers in {"both", "path"}:
            metadata["relay_path"] = "api_v1_e2ee"
        result["metadata"] = metadata
        return result
    return build


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    """Also protect these cases when collected by the repository's ordinary suite."""
    import socket
    import subprocess
    import encrypt
    import requests

    def denied(*_args, **_kwargs):
        raise AssertionError("K046 offline guard: forbidden I/O or real execution")

    class NoSocket(socket.socket):
        def __new__(cls, *_args, **_kwargs):
            denied()

    monkeypatch.setattr(socket, "socket", NoSocket)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "create_connection"):
        monkeypatch.setattr(socket, name, denied)
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(requests.sessions.Session, "send", denied)
    monkeypatch.setattr(encrypt, "generate_keys", lambda: (b"synthetic-private", b"synthetic-public"))
    monkeypatch.setattr(encrypt, "encrypt", denied)
    monkeypatch.setattr(encrypt, "decrypt", denied)
