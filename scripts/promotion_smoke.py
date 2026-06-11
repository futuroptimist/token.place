#!/usr/bin/env python3
"""Optional external staging/prod smoke checks for token.place promotion.

The module is safe to import in normal tests. The CLI intentionally refuses to
contact live services unless RUN_PROMOTION_SMOKE=1 and TOKENPLACE_SMOKE_BASE_URL
are both set.
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
ENABLE_ENV = "RUN_PROMOTION_SMOKE"
BASE_URL_ENV = "TOKENPLACE_SMOKE_BASE_URL"
ENVIRONMENT_ENV = "TOKENPLACE_SMOKE_ENV"
ALLOW_PROD_ENV = "TOKENPLACE_SMOKE_ALLOW_PROD"
DEFAULT_TIMEOUT_SECONDS = 10.0
JSON_ENDPOINTS = ("/livez", "/healthz", "/relay/diagnostics", "/api/v1/models")


class SmokeConfigError(ValueError):
    """Raised when smoke checks are not explicitly and safely configured."""


class SmokeCheckError(AssertionError):
    """Raised when an endpoint responds but violates the promotion contract."""


@dataclass(frozen=True)
class SmokeConfig:
    base_url: str
    environment: str
    allow_prod: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def _env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_base_url(raw_base_url: str) -> str:
    """Validate and normalize a configured HTTP(S) root base URL."""
    raw = raw_base_url.strip()
    if not raw:
        raise SmokeConfigError(f"{BASE_URL_ENV} must not be empty")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise SmokeConfigError(f"{BASE_URL_ENV} must use http:// or https://")
    if not parsed.netloc:
        raise SmokeConfigError(f"{BASE_URL_ENV} must include a host")
    if parsed.path.strip("/"):
        raise SmokeConfigError(f"{BASE_URL_ENV} must not include a path")
    if parsed.params or parsed.query or parsed.fragment:
        raise SmokeConfigError(
            f"{BASE_URL_ENV} must not include params, query, or fragment"
        )
    return f"{parsed.scheme}://{parsed.netloc.rstrip('/')}" + "/"


def _looks_like_prod(base_url: str, environment: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    env = environment.strip().lower()
    labels = set(host.split("."))
    production_hosts = {"token.place", "www.token.place"}
    return (
        env in {"prod", "production"}
        or host in production_hosts
        or (host.endswith(".token.place") and host != "staging.token.place")
        or bool(labels & {"prod", "production"})
    )


def config_from_env(env: dict[str, str] | None = None) -> SmokeConfig:
    """Build a safe smoke configuration from environment variables."""
    values = os.environ if env is None else env
    if not _env_truthy(values.get(ENABLE_ENV)):
        raise SmokeConfigError(
            f"Set {ENABLE_ENV}=1 to run external promotion smoke checks"
        )
    base_url = normalize_base_url(values.get(BASE_URL_ENV, ""))
    environment = values.get(ENVIRONMENT_ENV, "staging").strip().lower() or "staging"
    allow_prod = _env_truthy(values.get(ALLOW_PROD_ENV))
    if _looks_like_prod(base_url, environment) and not allow_prod:
        raise SmokeConfigError(
            f"Refusing production-looking target without {ALLOW_PROD_ENV}=1"
        )
    return SmokeConfig(
        base_url=base_url, environment=environment, allow_prod=allow_prod
    )


def endpoint_url(base_url: str, endpoint: str) -> str:
    """Join a normalized or raw base URL with a root-relative endpoint."""
    if not endpoint.startswith("/"):
        raise SmokeConfigError("endpoint must be root-relative")
    return urljoin(normalize_base_url(base_url), endpoint)


def validate_livez(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("status") != "alive":
        raise SmokeCheckError("/livez must return JSON status=alive")


def validate_healthz(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise SmokeCheckError("/healthz must return JSON status=ok")
    details = payload.get("details")
    if details:
        raise SmokeCheckError("/healthz must not include degraded details")


def validate_diagnostics(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise SmokeCheckError("/relay/diagnostics must return a JSON object")
    required_totals = (
        "total_registered_compute_nodes",
        "total_api_v1_registered_compute_nodes",
    )
    for key in required_totals:
        value = payload.get(key)
        if not isinstance(value, int) or value < 0:
            raise SmokeCheckError(
                f"/relay/diagnostics {key} must be a non-negative integer"
            )

    required_lists = (
        ("registered_compute_nodes", "total_registered_compute_nodes"),
        ("api_v1_registered_compute_nodes", "total_api_v1_registered_compute_nodes"),
    )
    for list_key, total_key in required_lists:
        nodes = payload.get(list_key)
        if not isinstance(nodes, list):
            raise SmokeCheckError(f"/relay/diagnostics {list_key} must be a list")
        if payload[total_key] != len(nodes):
            raise SmokeCheckError(
                f"/relay/diagnostics {total_key} must match {list_key} length"
            )


def validate_models(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("object") != "list":
        raise SmokeCheckError(
            "/api/v1/models must return an OpenAI-compatible list object"
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise SmokeCheckError("/api/v1/models data must be a list")
    if not all(isinstance(model, dict) for model in data):
        raise SmokeCheckError("/api/v1/models data entries must be objects")
    ids = [model.get("id") for model in data]
    if ids != [EXPECTED_MODEL_ID]:
        raise SmokeCheckError(
            f"/api/v1/models must expose exactly [{EXPECTED_MODEL_ID!r}], got {ids!r}"
        )
    owner = data[0].get("owned_by")
    if owner != "Meta":
        raise SmokeCheckError("/api/v1/models launch model must be owned_by=Meta")


VALIDATORS = {
    "/livez": validate_livez,
    "/healthz": validate_healthz,
    "/relay/diagnostics": validate_diagnostics,
    "/api/v1/models": validate_models,
}


def fetch_json(url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "tokenplace-promotion-smoke/1",
        },
    )
    try:
        with urlopen(
            request, timeout=timeout_seconds
        ) as response:  # nosec B310 - explicit operator URL only
            status = getattr(response, "status", response.getcode())
            body = response.read().decode("utf-8")
    except HTTPError as exc:  # pragma: no cover - unit tests exercise via fake fetchers
        raise SmokeCheckError(f"{url} returned HTTP {exc.code}") from exc
    except URLError as exc:  # pragma: no cover - unit tests exercise via fake fetchers
        raise SmokeCheckError(f"{url} request failed: {exc.reason}") from exc
    if status != 200:
        raise SmokeCheckError(f"{url} returned HTTP {status}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SmokeCheckError(f"{url} did not return valid JSON") from exc


def run_smoke_checks(
    config: SmokeConfig,
    *,
    fetcher=fetch_json,
    endpoints: tuple[str, ...] = JSON_ENDPOINTS,
) -> list[str]:
    """Run configured endpoint checks and return human-readable successes."""
    successes: list[str] = []
    for endpoint in endpoints:
        validator = VALIDATORS.get(endpoint)
        if validator is None:
            raise SmokeConfigError(f"Unsupported smoke endpoint: {endpoint}")
        payload = fetcher(
            endpoint_url(config.base_url, endpoint), config.timeout_seconds
        )
        validator(payload)
        successes.append(f"{endpoint} ok")
    return successes


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="per-request timeout in seconds (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = config_from_env()
        config = SmokeConfig(
            base_url=config.base_url,
            environment=config.environment,
            allow_prod=config.allow_prod,
            timeout_seconds=args.timeout,
        )
        successes = run_smoke_checks(config)
    except (SmokeConfigError, SmokeCheckError) as exc:
        print(f"promotion smoke failed: {exc}", file=sys.stderr)
        return 2
    for line in successes:
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
