"""Minimal stdlib-backed subset of the ``requests`` API for desktop bridge runtime.

This module exists so packaged desktop Python bridge scripts do not depend on
user-global ``requests`` installations. It intentionally implements only the
surface used by token.place bridge startup/runtime paths.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional
from urllib import error as urlerror
from urllib import request as urlrequest


class RequestException(Exception):
    """Base request compatibility exception."""


class ConnectionError(RequestException):
    """Raised on transport/connectivity failures."""


class Timeout(RequestException):
    """Raised on transport timeouts."""


class HTTPError(RequestException):
    """Raised when ``raise_for_status`` sees a non-2xx response."""


@dataclass
class Response:
    status_code: int
    content: bytes
    headers: Dict[str, str]
    reason: str = ""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"{self.status_code} {self.reason}".strip())

    def iter_lines(self, decode_unicode: bool = False) -> Iterator[str | bytes]:
        for line in self.content.splitlines():
            if decode_unicode:
                yield line.decode("utf-8", errors="replace")
            else:
                yield line


def _perform(
    method: str,
    url: str,
    *,
    json_payload: Optional[Dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> Response:
    headers = {"Accept": "application/json"}
    body: bytes | None = None
    if json_payload is not None:
        body = json.dumps(json_payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, method=method, data=body, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            return Response(
                status_code=int(getattr(resp, "status", 200)),
                content=raw,
                headers={k: v for k, v in resp.headers.items()},
                reason=getattr(resp, "reason", "") or "",
            )
    except urlerror.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        return Response(
            status_code=int(getattr(exc, "code", 500)),
            content=raw,
            headers={k: v for k, v in getattr(exc, "headers", {}).items()},
            reason=getattr(exc, "reason", "") or "",
        )
    except urlerror.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or isinstance(reason, socket.timeout):
            raise Timeout(str(exc)) from exc
        raise ConnectionError(str(exc)) from exc
    except TimeoutError as exc:
        raise Timeout(str(exc)) from exc


def get(url: str, *, timeout: Optional[float] = None, stream: bool = False) -> Response:
    del stream  # compatibility arg
    return _perform("GET", url, timeout=timeout)


def post(url: str, *, json: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Response:
    return _perform("POST", url, json_payload=json, timeout=timeout)
