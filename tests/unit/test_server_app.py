"""Tests for the legacy ``server.server_app`` compatibility shim."""

from __future__ import annotations

import types

import server.server_app as shim


class _CanonicalStub:
    def __init__(self):
        self.calls = []
        self.ServerApp = object()

    def parse_args(self):
        self.calls.append("parse_args")
        return types.SimpleNamespace(server_port=3000)

    def main(self):
        self.calls.append("main")
        return 123


def test_parse_args_delegates_to_canonical_module(monkeypatch):
    stub = _CanonicalStub()
    monkeypatch.setattr(shim, "_CANONICAL_MODULE", stub)

    args = shim.parse_args()

    assert args.server_port == 3000
    assert stub.calls == ["parse_args"]


def test_main_delegates_to_canonical_module(monkeypatch):
    stub = _CanonicalStub()
    monkeypatch.setattr(shim, "_CANONICAL_MODULE", stub)

    result = shim.main()

    assert result == 123
    assert stub.calls == ["main"]


def test_unknown_attributes_delegate_to_canonical_module(monkeypatch):
    stub = _CanonicalStub()
    stub.some_attr = "delegated"
    monkeypatch.setattr(shim, "_CANONICAL_MODULE", stub)

    assert shim.some_attr == "delegated"


def test_server_app_symbol_is_canonical():
    assert shim.ServerApp is shim._load_canonical_module().ServerApp
