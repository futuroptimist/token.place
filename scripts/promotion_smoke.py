#!/usr/bin/env python3
"""Optional staging/prod promotion JSON endpoint smoke checks.

The script is safe by default: it exits without network access unless
RUN_PROMOTION_SMOKE=1 and TOKENPLACE_SMOKE_BASE_URL are set. Production targets
also require TOKENPLACE_SMOKE_ENV=prod and TOKENPLACE_SMOKE_ALLOW_PROD=1.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import requests

EXPECTED_MODEL_ID = "llama-3.1-8b-instruct"
ENDPOINTS = ("/livez", "/healthz", "/relay/diagnostics", "/api/v1/models")


class PromotionSmokeError(RuntimeError):
    """Raised when a promotion smoke assertion fails."""


@dataclass(frozen=True)
class SmokeConfig:
    base_url: str
    environment: str = "staging"
    allow_prod: bool = False
    enabled: bool = False
    timeout: float = 10.0


def _env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def normalize_base_url(raw_url: str) -> str:
    """Return a normalized http(s) base URL without a trailing slash."""
    candidate = raw_url.strip()
    if not candidate:
        raise PromotionSmokeError("TOKENPLACE_SMOKE_BASE_URL must not be empty")

    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PromotionSmokeError(
            "TOKENPLACE_SMOKE_BASE_URL must be an absolute http(s) URL"
        )
    if parsed.params or parsed.query or parsed.fragment:
        raise PromotionSmokeError(
            "TOKENPLACE_SMOKE_BASE_URL must not include params, query, or fragment"
        )

    path = parsed.path.rstrip("/")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def build_config(env: Mapping[str, str] | None = None) -> SmokeConfig:
    env = env or os.environ
    enabled = _env_truthy(env.get("RUN_PROMOTION_SMOKE"))
    base_url_raw = env.get("TOKENPLACE_SMOKE_BASE_URL", "")
    environment = (
        env.get("TOKENPLACE_SMOKE_ENV", "staging").strip().lower() or "staging"
    )
    allow_prod = _env_truthy(env.get("TOKENPLACE_SMOKE_ALLOW_PROD"))

    if not enabled:
        return SmokeConfig(
            base_url="", environment=environment, allow_prod=allow_prod, enabled=False
        )
    if environment not in {"staging", "prod", "production"}:
        raise PromotionSmokeError("TOKENPLACE_SMOKE_ENV must be 'staging' or 'prod'")
    if environment in {"prod", "production"} and not allow_prod:
        raise PromotionSmokeError(
            "Production smoke checks require TOKENPLACE_SMOKE_ALLOW_PROD=1"
        )

    return SmokeConfig(
        base_url=normalize_base_url(base_url_raw),
        environment="prod" if environment == "production" else environment,
        allow_prod=allow_prod,
        enabled=True,
    )


def endpoint_url(base_url: str, endpoint: str) -> str:
    if not endpoint.startswith("/"):
        raise PromotionSmokeError(f"Endpoint must start with '/': {endpoint}")
    return f"{normalize_base_url(base_url)}{endpoint}"


def fetch_json(url: str, *, timeout: float = 10.0) -> Any:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PromotionSmokeError("Smoke endpoint URL must be absolute http(s)")
    if parsed.params or parsed.query or parsed.fragment:
        raise PromotionSmokeError(
            "Smoke endpoint URL must not include params, query, or fragment"
        )
    normalized_url = urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", "", "")
    )
    try:
        response = requests.get(
            normalized_url,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except (
        requests.RequestException
    ) as exc:  # pragma: no cover - exercised by live failures
        raise PromotionSmokeError(f"{normalized_url} request failed: {exc}") from exc

    if response.status_code != 200:
        raise PromotionSmokeError(
            f"{normalized_url} returned HTTP {response.status_code}"
        )
    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type.lower():
        raise PromotionSmokeError(
            f"{normalized_url} returned non-JSON content type {content_type!r}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise PromotionSmokeError(
            f"{normalized_url} returned invalid JSON: {exc}"
        ) from exc


def validate_livez(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("status") != "alive":
        raise PromotionSmokeError("/livez must return {'status': 'alive'}")


def validate_healthz(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise PromotionSmokeError("/healthz must return a JSON object")
    if payload.get("status") != "ok":
        raise PromotionSmokeError("/healthz status must be 'ok'")
    known_servers = payload.get("knownServers")
    if not isinstance(known_servers, int) or known_servers < 0:
        raise PromotionSmokeError(
            "/healthz knownServers must be a non-negative integer"
        )
    registered = payload.get("registeredServers")
    if not isinstance(registered, list):
        raise PromotionSmokeError("/healthz registeredServers must be a list")
    if len(registered) != known_servers:
        raise PromotionSmokeError(
            "/healthz knownServers must match registeredServers length"
        )


def validate_diagnostics(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise PromotionSmokeError("/relay/diagnostics must return a JSON object")
    nodes = payload.get("registered_compute_nodes")
    api_v1_nodes = payload.get("api_v1_registered_compute_nodes")
    if not isinstance(nodes, list) or not isinstance(api_v1_nodes, list):
        raise PromotionSmokeError("/relay/diagnostics node lists must be arrays")
    if payload.get("total_registered_compute_nodes") != len(nodes):
        raise PromotionSmokeError(
            "/relay/diagnostics total_registered_compute_nodes mismatch"
        )
    if payload.get("total_api_v1_registered_compute_nodes") != len(api_v1_nodes):
        raise PromotionSmokeError(
            "/relay/diagnostics total_api_v1_registered_compute_nodes mismatch"
        )


def validate_models(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("object") != "list":
        raise PromotionSmokeError(
            "/api/v1/models must return an OpenAI-compatible list object"
        )
    models = payload.get("data")
    if not isinstance(models, list):
        raise PromotionSmokeError("/api/v1/models data must be a list")
    if len(models) != 1:
        raise PromotionSmokeError("/api/v1/models must expose exactly one public model")
    model = models[0]
    if not isinstance(model, dict):
        raise PromotionSmokeError("/api/v1/models model entry must be an object")
    if model.get("id") != EXPECTED_MODEL_ID:
        raise PromotionSmokeError(
            f"/api/v1/models must expose only {EXPECTED_MODEL_ID}"
        )
    if "token.place" in str(model.get("owned_by", "")).lower():
        raise PromotionSmokeError(
            "/api/v1/models owned_by must not claim token.place ownership"
        )


VALIDATORS: dict[str, Callable[[Any], None]] = {
    "/livez": validate_livez,
    "/healthz": validate_healthz,
    "/relay/diagnostics": validate_diagnostics,
    "/api/v1/models": validate_models,
}


def run_smoke(
    config: SmokeConfig,
    *,
    fetcher: Callable[[str], Any] | None = None,
) -> dict[str, str]:
    if not config.enabled:
        return {"status": "skipped", "reason": "RUN_PROMOTION_SMOKE is not enabled"}

    fetcher = fetcher or (lambda url: fetch_json(url, timeout=config.timeout))
    results: dict[str, str] = {}
    for endpoint in ENDPOINTS:
        url = endpoint_url(config.base_url, endpoint)
        payload = fetcher(url)
        VALIDATORS[endpoint](payload)
        results[endpoint] = "ok"
    return results


def main() -> int:
    try:
        config = build_config()
        results = run_smoke(config)
    except PromotionSmokeError as exc:
        print(f"promotion smoke failed: {exc}", file=sys.stderr)
        return 1

    if results.get("status") == "skipped":
        print(f"promotion smoke skipped: {results['reason']}")
        return 0

    print(
        json.dumps(
            {
                "base_url": config.base_url,
                "environment": config.environment,
                "results": results,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
