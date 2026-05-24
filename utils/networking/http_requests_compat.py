"""Requests compatibility layer with stdlib fallback for desktop runtime paths."""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

try:  # pragma: no cover - exercised when requests is installed
    import requests as _requests
except ModuleNotFoundError:  # pragma: no cover - fallback covered in dedicated tests
    _requests = None


if _requests is not None:
    requests = _requests
else:
    class RequestException(Exception):
        pass

    class ConnectionError(RequestException):
        pass

    class Timeout(RequestException):
        pass

    @dataclass
    class _Response:
        status_code: int
        _body: Optional[bytes] = None
        _handle: Any = None
        headers: Optional[Dict[str, str]] = None

        @property
        def text(self) -> str:
            if self._body is None:
                self._body = self._handle.read() if self._handle is not None else b""
            return self._body.decode("utf-8", errors="replace")

        def json(self) -> Dict[str, Any]:
            return json.loads(self.text)

        def iter_lines(self) -> Iterable[bytes]:
            if self._body is None:
                self._body = self._handle.read() if self._handle is not None else b""
            return self._body.splitlines()

        def iter_content(self, chunk_size: int = 1) -> Iterable[bytes]:
            if chunk_size <= 0:
                chunk_size = 1
            if self._handle is None:
                payload = self._body or b""
                for idx in range(0, len(payload), chunk_size):
                    yield payload[idx:idx + chunk_size]
                return
            while True:
                chunk = self._handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk

        def close(self) -> None:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def _normalize_headers(resp: Any) -> Dict[str, str]:
        hdrs = getattr(resp, "headers", None)
        if hdrs is None:
            return {}
        return {str(k).lower(): str(v) for k, v in hdrs.items()}

    def _request(
        method: str,
        url: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
        stream: bool = False,
    ) -> _Response:
        body = None
        req_headers = dict(headers or {})
        if json_payload is not None:
            body = json.dumps(json_payload).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        req = urllib_request.Request(url=url, data=body, headers=req_headers, method=method)
        try:
            resp = urllib_request.urlopen(req, timeout=timeout)  # nosec B310 - relay URLs are app-configured network endpoints
            if stream:
                return _Response(
                    status_code=getattr(resp, "status", 200),
                    _handle=resp,
                    headers=_normalize_headers(resp),
                )
            with resp:
                return _Response(
                    status_code=getattr(resp, "status", 200),
                    _body=resp.read(),
                    headers=_normalize_headers(resp),
                )
        except urllib_error.HTTPError as exc:
            return _Response(status_code=exc.code, _body=exc.read(), headers=_normalize_headers(exc))
        except urllib_error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, socket.timeout):
                raise Timeout(str(exc)) from exc
            raise ConnectionError(str(exc)) from exc

    class _CompatRequests:
        RequestException = RequestException
        ConnectionError = ConnectionError
        Timeout = Timeout

        @staticmethod
        def post(url: str, json: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, headers: Optional[Dict[str, str]] = None, **_: Any) -> _Response:
            return _request("POST", url, json_payload=json, headers=headers, timeout=timeout)

        @staticmethod
        def get(url: str, timeout: Optional[float] = None, headers: Optional[Dict[str, str]] = None, stream: bool = False, **_: Any) -> _Response:
            return _request("GET", url, headers=headers, timeout=timeout, stream=stream)

    requests = _CompatRequests()
