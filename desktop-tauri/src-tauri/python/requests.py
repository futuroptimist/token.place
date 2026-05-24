"""Minimal requests-compatible shim for desktop bridge runtime.

This module intentionally implements only the subset used by the desktop
compute-node bridge startup/polling path so packaged apps do not depend on a
system-installed third-party ``requests`` package.
"""

from __future__ import annotations

import json as _json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class RequestException(Exception):
    """Base request error."""


class ConnectionError(RequestException):
    """Raised when network connection fails."""


class Timeout(RequestException):
    """Raised when a request times out."""


class HTTPError(RequestException):
    """Raised for HTTP status failures."""


@dataclass
class Response:
    status_code: int
    _body: bytes

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return _json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"HTTP {self.status_code}: {self.text}")


def _request(method: str, url: str, *, json: Any = None, timeout: float | None = None) -> Response:
    headers = {"Accept": "application/json"}
    data = None
    if json is not None:
        data = _json.dumps(json).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return Response(status_code=resp.status, _body=resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return Response(status_code=int(exc.code), _body=body)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, socket.timeout):
            raise Timeout(str(exc)) from exc
        raise ConnectionError(str(exc)) from exc
    except TimeoutError as exc:
        raise Timeout(str(exc)) from exc


def get(url: str, *, timeout: float | None = None, **_kwargs: Any) -> Response:
    return _request("GET", url, timeout=timeout)


def post(url: str, *, json: Any = None, timeout: float | None = None, **_kwargs: Any) -> Response:
    return _request("POST", url, json=json, timeout=timeout)
