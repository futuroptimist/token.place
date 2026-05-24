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
        _body: bytes

        @property
        def text(self) -> str:
            return self._body.decode("utf-8", errors="replace")

        def json(self) -> Dict[str, Any]:
            return json.loads(self.text)

        def iter_lines(self) -> Iterable[bytes]:
            return self._body.splitlines()

    def _request(method: str, url: str, *, json_payload: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None) -> _Response:
        body = None
        req_headers = dict(headers or {})
        if json_payload is not None:
            body = json.dumps(json_payload).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        req = urllib_request.Request(url=url, data=body, headers=req_headers, method=method)
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - relay URLs are app-configured network endpoints
                return _Response(status_code=getattr(resp, "status", 200), _body=resp.read())
        except urllib_error.HTTPError as exc:
            return _Response(status_code=exc.code, _body=exc.read())
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
        def get(url: str, timeout: Optional[float] = None, headers: Optional[Dict[str, str]] = None, **_: Any) -> _Response:
            return _request("GET", url, headers=headers, timeout=timeout)

    requests = _CompatRequests()
