"""Tests for requests compatibility fallback used by desktop bridge runtime."""

from __future__ import annotations

import importlib
import io
import sys


class _FakeHTTPResponse:
    def __init__(self, body: bytes, headers: dict[str, str], status: int = 200) -> None:
        self.status = status
        self._buffer = io.BytesIO(body)
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:
        self._buffer.close()


def test_requests_compat_fallback_supports_streaming_download_surface(monkeypatch):
    monkeypatch.setitem(sys.modules, 'requests', None)
    module = importlib.import_module('utils.networking.http_requests_compat')
    module = importlib.reload(module)

    fake_response = _FakeHTTPResponse(
        body=b'abcdefghij',
        headers={'content-length': '10', 'x-test': 'ok'},
        status=200,
    )

    monkeypatch.setattr(
        module.urllib_request,
        'urlopen',
        lambda *_args, **_kwargs: fake_response,
    )

    response = module.requests.get('https://example.test/model.bin', stream=True, timeout=1)

    assert response.status_code == 200
    assert response.headers.get('content-length') == '10'
    chunks = list(response.iter_content(chunk_size=4))
    assert chunks == [b'abcd', b'efgh', b'ij']
    response.close()


def test_requests_compat_fallback_exposes_exception_types(monkeypatch):
    monkeypatch.setitem(sys.modules, 'requests', None)
    module = importlib.import_module('utils.networking.http_requests_compat')
    module = importlib.reload(module)

    assert hasattr(module.requests, 'post')
    assert hasattr(module.requests, 'ConnectionError')
    assert hasattr(module.requests, 'Timeout')
