#!/usr/bin/env python3
"""Optional staging/prod promotion smoke checks for token.place.

The helper is safe-by-default: it only runs when RUN_PROMOTION_SMOKE=1 and a
TOKENPLACE_SMOKE_BASE_URL target are both supplied. It checks read-only JSON
endpoints and avoids sending prompts or secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

EXPECTED_MODEL_ID = "llama-3.1-8b-instruct"
JSON_ENDPOINTS = ("/livez", "/healthz", "/relay/diagnostics", "/api/v1/models")


class SmokeError(RuntimeError):
    """Raised when a promotion smoke check fails."""


@dataclass(frozen=True)
class EndpointResult:
    path: str
    status: int
    payload: Any


def normalize_base_url(raw_url: str) -> str:
    """Return a normalized http(s) base URL without query, fragment, or path."""
    value = raw_url.strip()
    if not value:
        raise SmokeError("TOKENPLACE_SMOKE_BASE_URL must not be empty")

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SmokeError("TOKENPLACE_SMOKE_BASE_URL must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise SmokeError("TOKENPLACE_SMOKE_BASE_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise SmokeError("TOKENPLACE_SMOKE_BASE_URL must not contain query strings or fragments")

    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def endpoint_url(base_url: str, path: str) -> str:
    """Build a smoke endpoint URL from a normalized base and absolute path."""
    if not path.startswith("/"):
        raise SmokeError(f"Smoke endpoint path must be absolute: {path}")
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def fetch_json(base_url: str, path: str, timeout: float = 10.0) -> EndpointResult:
    """Fetch a JSON endpoint and return the decoded payload."""
    request = Request(
        endpoint_url(base_url, path),
        headers={"Accept": "application/json", "User-Agent": "tokenplace-promotion-smoke/0.1"},
        method="GET",
    )
    try:
        # normalize_base_url restricts smoke targets to operator-supplied http(s) URLs.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            status = int(response.status)
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except HTTPError as exc:
        raise SmokeError(f"{path} returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise SmokeError(f"{path} request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SmokeError(f"{path} request timed out") from exc

    if not 200 <= status < 300:
        raise SmokeError(f"{path} returned HTTP {status}")
    if "json" not in content_type.lower():
        raise SmokeError(f"{path} did not return JSON content (Content-Type: {content_type or 'missing'})")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError(f"{path} returned invalid JSON") from exc
    return EndpointResult(path=path, status=status, payload=payload)


def assert_health_payload(path: str, payload: Any) -> None:
    """Validate a lightweight health/liveness payload."""
    if not isinstance(payload, dict):
        raise SmokeError(f"{path} payload must be a JSON object")
    status = str(payload.get("status", "")).lower()
    if status and status not in {"ok", "healthy", "live"}:
        raise SmokeError(f"{path} reported unhealthy status: {payload.get('status')!r}")


def assert_diagnostics_payload(payload: Any) -> None:
    """Validate relay diagnostics count shape without exposing private material."""
    if not isinstance(payload, dict):
        raise SmokeError("/relay/diagnostics payload must be a JSON object")
    for field in ("registered_compute_nodes", "total_registered_compute_nodes"):
        if field not in payload:
            raise SmokeError(f"/relay/diagnostics missing {field}")
    nodes = payload["registered_compute_nodes"]
    total = payload["total_registered_compute_nodes"]
    if not isinstance(nodes, list):
        raise SmokeError("/relay/diagnostics registered_compute_nodes must be a list")
    if not isinstance(total, int) or total < 0:
        raise SmokeError("/relay/diagnostics total_registered_compute_nodes must be a non-negative integer")
    if total != len(nodes):
        raise SmokeError("/relay/diagnostics total_registered_compute_nodes does not match registered_compute_nodes length")
    api_v1_total = payload.get("total_api_v1_registered_compute_nodes")
    api_v1_nodes = payload.get("api_v1_registered_compute_nodes")
    if api_v1_total is not None and (not isinstance(api_v1_total, int) or api_v1_total < 0):
        raise SmokeError("/relay/diagnostics total_api_v1_registered_compute_nodes must be non-negative when present")
    if api_v1_nodes is not None and not isinstance(api_v1_nodes, list):
        raise SmokeError("/relay/diagnostics api_v1_registered_compute_nodes must be a list when present")
    if isinstance(api_v1_nodes, list) and isinstance(api_v1_total, int) and api_v1_total != len(api_v1_nodes):
        raise SmokeError("/relay/diagnostics total_api_v1_registered_compute_nodes does not match api_v1_registered_compute_nodes length")


def assert_models_payload(payload: Any) -> None:
    """Validate the frozen public API v1 launch model catalog."""
    if not isinstance(payload, dict):
        raise SmokeError("/api/v1/models payload must be a JSON object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise SmokeError("/api/v1/models data must be a list")
    ids = [model.get("id") for model in data if isinstance(model, dict)]
    if ids != [EXPECTED_MODEL_ID]:
        raise SmokeError(
            "/api/v1/models must return exactly one public model: "
            f"{EXPECTED_MODEL_ID}; got {ids!r}"
        )
    owner = data[0].get("owned_by")
    if isinstance(owner, str) and "token.place" in owner.lower():
        raise SmokeError('/api/v1/models must not claim the launch model is "owned by token.place"')


def validate_endpoint_result(result: EndpointResult) -> None:
    if result.path in {"/livez", "/healthz"}:
        assert_health_payload(result.path, result.payload)
    elif result.path == "/relay/diagnostics":
        assert_diagnostics_payload(result.payload)
    elif result.path == "/api/v1/models":
        assert_models_payload(result.payload)
    else:
        raise SmokeError(f"Unexpected smoke endpoint: {result.path}")


def run_smoke(base_url: str, timeout: float = 10.0) -> list[EndpointResult]:
    normalized = normalize_base_url(base_url)
    results = [fetch_json(normalized, path, timeout=timeout) for path in JSON_ENDPOINTS]
    for result in results:
        validate_endpoint_result(result)
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run optional token.place promotion smoke checks.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TOKENPLACE_SMOKE_BASE_URL", ""),
        help="Staging/prod relay base URL. Defaults to TOKENPLACE_SMOKE_BASE_URL.",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout in seconds.")
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("RUN_PROMOTION_SMOKE") != "1":
        print("Skipping promotion smoke checks: set RUN_PROMOTION_SMOKE=1 to enable.")
        return 0

    args = _build_parser().parse_args(argv)
    if not args.base_url:
        print("Skipping promotion smoke checks: set TOKENPLACE_SMOKE_BASE_URL or pass --base-url.")
        return 0

    try:
        results = run_smoke(args.base_url, timeout=args.timeout)
    except SmokeError as exc:
        print(f"Promotion smoke failed: {exc}", file=sys.stderr)
        return 1

    for result in results:
        print(f"ok {result.path} HTTP {result.status}")
    print("Promotion smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
