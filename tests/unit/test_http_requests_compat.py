"""Tests for requests compatibility fallback used by desktop bridge runtime."""

from __future__ import annotations

import importlib
import sys


def test_requests_compat_fallback_imports_without_requests(monkeypatch):
    monkeypatch.setitem(sys.modules, 'requests', None)
    module = importlib.import_module('utils.networking.http_requests_compat')
    module = importlib.reload(module)

    assert hasattr(module, 'requests')
    assert hasattr(module.requests, 'post')
    assert hasattr(module.requests, 'ConnectionError')
