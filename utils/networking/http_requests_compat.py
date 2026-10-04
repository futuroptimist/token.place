"""Deterministic stdlib HTTP compatibility layer for desktop bridge runtime paths."""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from utils.networking.relay_credentials import canonical_relay_url, RelayCredentialError


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


class _NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None



def _request(
    method: str,
    url: str,
    *,
    json_payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[float] = None,
    stream: bool = False,
    allow_redirects: bool = True,
) -> _Response:
    body = None
    req_headers = dict(headers or {})
    if json_payload is not None:
        body = json.dumps(json_payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib_request.Request(url=url, data=body, headers=req_headers, method=method)
    try:
        credential_bearing = any(
            key.lower() in {"x-relay-server-token", "authorization", "cookie"}
            for key in req_headers
        ) or bool(json_payload and "control_credential" in json_payload)
        if credential_bearing:
            try:
                canonical_relay_url(url)
            except RelayCredentialError:
                raise RequestException("Credential-bearing requests require a valid HTTPS or explicit loopback URL.") from None
        if credential_bearing or not allow_redirects:
            resp = urllib_request.build_opener(_NoRedirect()).open(req, timeout=timeout)
        else:
            resp = urllib_request.urlopen(req, timeout=timeout)  # nosec B310 - app-configured endpoints
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
    except (socket.timeout, TimeoutError) as exc:
        raise Timeout(str(exc)) from exc


class _CompatRequests:
    RequestException = RequestException
    ConnectionError = ConnectionError
    Timeout = Timeout

    @staticmethod
    def post(url: str, json: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None, headers: Optional[Dict[str, str]] = None, allow_redirects: bool = True, **_: Any) -> _Response:
        return _request("POST", url, json_payload=json, headers=headers, timeout=timeout, allow_redirects=allow_redirects)

    @staticmethod
    def get(url: str, timeout: Optional[float] = None, headers: Optional[Dict[str, str]] = None, stream: bool = False, allow_redirects: bool = True, **_: Any) -> _Response:
        return _request("GET", url, headers=headers, timeout=timeout, stream=stream, allow_redirects=allow_redirects)


requests = _CompatRequests()
