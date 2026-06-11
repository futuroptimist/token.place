#!/usr/bin/env python3
"""Optional staging/prod promotion smoke checks for token.place.

The harness is intentionally opt-in. It only performs network requests when
RUN_PROMOTION_SMOKE=1 is set and TOKENPLACE_SMOKE_BASE_URL points at the target
relay. Production additionally requires TOKENPLACE_SMOKE_ALLOW_PROD=1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

EXPECTED_MODEL_ID = "llama-3.1-8b-instruct"
PRODUCTION_HOSTS = {"token.place", "www.token.place"}
JSON_ENDPOINTS = ("/livez", "/healthz", "/relay/diagnostics", "/api/v1/models")


class SmokeCheckError(RuntimeError):
    """Raised when a smoke check response violates the promotion contract."""


@dataclass(frozen=True)
class SmokeResult:
    endpoint: str
    status: str


def normalize_base_url(raw_url: str) -> str:
    """Return a scheme/netloc-only base URL with trailing slash and path removed."""

    candidate = raw_url.strip()
    if not candidate:
        raise SmokeCheckError("TOKENPLACE_SMOKE_BASE_URL must not be empty")
    parsed = urllib.parse.urlparse(candidate)
    if not parsed.scheme:
        candidate = f"https://{candidate}"
        parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        raise SmokeCheckError(f"unsupported URL scheme: {parsed.scheme}")
    if not parsed.netloc:
        raise SmokeCheckError("base URL must include a host")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def is_production_url(base_url: str) -> bool:
    """Return True when the base URL targets the public production host."""

    host = urllib.parse.urlparse(base_url).hostname or ""
    return host.lower() in PRODUCTION_HOSTS


def endpoint_url(base_url: str, endpoint: str) -> str:
    """Join a normalized base URL and an absolute endpoint path."""

    if not endpoint.startswith("/"):
        raise SmokeCheckError(f"endpoint must start with '/': {endpoint}")
    return urllib.parse.urljoin(f"{base_url}/", endpoint.lstrip("/"))


def fetch_json(base_url: str, endpoint: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch one smoke endpoint as JSON."""

    url = endpoint_url(base_url, endpoint)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - explicit opt-in smoke target.
            status = getattr(response, "status", response.getcode())
            content_type = response.headers.get("Content-Type", "")
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # pragma: no cover - exercised by integration use.
        raise SmokeCheckError(f"{endpoint} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover - exercised by integration use.
        raise SmokeCheckError(f"{endpoint} request failed: {exc.reason}") from exc

    if status != 200:
        raise SmokeCheckError(f"{endpoint} returned HTTP {status}")
    if "json" not in content_type.lower():
        raise SmokeCheckError(f"{endpoint} returned non-JSON content type {content_type!r}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SmokeCheckError(f"{endpoint} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SmokeCheckError(f"{endpoint} returned a non-object JSON payload")
    return payload


def validate_livez(payload: dict[str, Any]) -> None:
    if payload.get("status") != "alive":
        raise SmokeCheckError("/livez status must be 'alive'")


def validate_healthz(payload: dict[str, Any]) -> None:
    if payload.get("status") != "ok":
        raise SmokeCheckError("/healthz status must be 'ok'")
    registered = payload.get("registeredServers")
    if not isinstance(registered, list):
        raise SmokeCheckError("/healthz registeredServers must be a list")


def validate_diagnostics(payload: dict[str, Any]) -> None:
    registered = payload.get("registered_compute_nodes")
    api_v1_registered = payload.get("api_v1_registered_compute_nodes")
    total = payload.get("total_registered_compute_nodes")
    api_v1_total = payload.get("total_api_v1_registered_compute_nodes")
    if not isinstance(registered, list):
        raise SmokeCheckError("/relay/diagnostics registered_compute_nodes must be a list")
    if not isinstance(api_v1_registered, list):
        raise SmokeCheckError("/relay/diagnostics api_v1_registered_compute_nodes must be a list")
    if total != len(registered):
        raise SmokeCheckError("/relay/diagnostics total_registered_compute_nodes does not match node list")
    if api_v1_total != len(api_v1_registered):
        raise SmokeCheckError("/relay/diagnostics total_api_v1_registered_compute_nodes does not match node list")
    if api_v1_total > total:
        raise SmokeCheckError("/relay/diagnostics API v1 node count cannot exceed total live node count")


def validate_models(payload: dict[str, Any]) -> None:
    if payload.get("object") != "list":
        raise SmokeCheckError("/api/v1/models object must be 'list'")
    models = payload.get("data")
    if not isinstance(models, list):
        raise SmokeCheckError("/api/v1/models data must be a list")
    if len(models) != 1:
        raise SmokeCheckError("/api/v1/models must expose exactly one public model")
    model = models[0]
    if not isinstance(model, dict):
        raise SmokeCheckError("/api/v1/models data[0] must be an object")
    if model.get("id") != EXPECTED_MODEL_ID:
        raise SmokeCheckError(f"/api/v1/models must expose {EXPECTED_MODEL_ID}")
    owner = str(model.get("owned_by", ""))
    if owner != "Meta":
        raise SmokeCheckError("/api/v1/models owned_by must be 'Meta'")
    if "token.place" in owner.lower():
        raise SmokeCheckError("/api/v1/models must not claim token.place owns the launch model")
    model_ids = [str(item.get("id", "")) for item in models if isinstance(item, dict)]
    if any(":alignment" in model_id for model_id in model_ids):
        raise SmokeCheckError("/api/v1/models must not expose alignment model variants")


def run_smoke(base_url: str, *, timeout: float = 10.0) -> list[SmokeResult]:
    normalized = normalize_base_url(base_url)
    validators = {
        "/livez": validate_livez,
        "/healthz": validate_healthz,
        "/relay/diagnostics": validate_diagnostics,
        "/api/v1/models": validate_models,
    }
    results: list[SmokeResult] = []
    for endpoint in JSON_ENDPOINTS:
        payload = fetch_json(normalized, endpoint, timeout=timeout)
        validators[endpoint](payload)
        results.append(SmokeResult(endpoint=endpoint, status="ok"))
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run opt-in token.place promotion smoke checks")
    parser.add_argument("--base-url", default=os.environ.get("TOKENPLACE_SMOKE_BASE_URL", ""))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("TOKENPLACE_SMOKE_TIMEOUT", "10")))
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if os.environ.get("RUN_PROMOTION_SMOKE") != "1":
        print("SKIP: set RUN_PROMOTION_SMOKE=1 and TOKENPLACE_SMOKE_BASE_URL to run promotion smoke checks")
        return 0

    try:
        base_url = normalize_base_url(args.base_url)
        if is_production_url(base_url) and os.environ.get("TOKENPLACE_SMOKE_ALLOW_PROD") != "1":
            raise SmokeCheckError(
                "refusing to smoke-test production without TOKENPLACE_SMOKE_ALLOW_PROD=1"
            )
        results = run_smoke(base_url, timeout=args.timeout)
    except SmokeCheckError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    for result in results:
        print(f"PASS: {result.endpoint} {result.status}")
    print(f"PASS: promotion smoke checks completed for {base_url}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
