from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "promotion_smoke", Path("scripts/promotion_smoke.py")
)
promotion_smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promotion_smoke
assert SPEC.loader is not None
SPEC.loader.exec_module(promotion_smoke)


def test_config_from_env_requires_explicit_enablement() -> None:
    with pytest.raises(promotion_smoke.SmokeConfigError, match="RUN_PROMOTION_SMOKE"):
        promotion_smoke.config_from_env(
            {"TOKENPLACE_SMOKE_BASE_URL": "https://staging.token.place"}
        )


def test_config_from_env_normalizes_staging_url() -> None:
    config = promotion_smoke.config_from_env(
        {
            "RUN_PROMOTION_SMOKE": "1",
            "TOKENPLACE_SMOKE_ENV": "staging",
            "TOKENPLACE_SMOKE_BASE_URL": "https://staging.token.place///",
        }
    )

    assert config.base_url == "https://staging.token.place/"
    assert config.environment == "staging"
    assert config.allow_prod is False


def test_config_from_env_refuses_production_without_acknowledgement() -> None:
    with pytest.raises(promotion_smoke.SmokeConfigError, match="TOKENPLACE_SMOKE_ALLOW_PROD"):
        promotion_smoke.config_from_env(
            {
                "RUN_PROMOTION_SMOKE": "1",
                "TOKENPLACE_SMOKE_ENV": "production",
                "TOKENPLACE_SMOKE_BASE_URL": "https://token.place",
            }
        )


def test_config_from_env_allows_production_with_explicit_acknowledgement() -> None:
    config = promotion_smoke.config_from_env(
        {
            "RUN_PROMOTION_SMOKE": "1",
            "TOKENPLACE_SMOKE_ENV": "production",
            "TOKENPLACE_SMOKE_ALLOW_PROD": "1",
            "TOKENPLACE_SMOKE_BASE_URL": "https://token.place",
        }
    )

    assert config.base_url == "https://token.place/"
    assert config.environment == "production"
    assert config.allow_prod is True


def test_endpoint_url_rejects_relative_endpoint_without_leading_slash() -> None:
    with pytest.raises(promotion_smoke.SmokeConfigError, match="root-relative"):
        promotion_smoke.endpoint_url("https://staging.token.place", "api/v1/models")


def test_validate_models_requires_exact_launch_model() -> None:
    promotion_smoke.validate_models(
        {
            "object": "list",
            "data": [
                {
                    "id": "llama-3.1-8b-instruct",
                    "object": "model",
                    "owned_by": "Meta",
                }
            ],
        }
    )

    with pytest.raises(promotion_smoke.SmokeCheckError, match="exactly"):
        promotion_smoke.validate_models(
            {
                "object": "list",
                "data": [
                    {"id": "llama-3.1-8b-instruct", "owned_by": "Meta"},
                    {"id": "llama-3.1-8b-instruct:alignment", "owned_by": "Meta"},
                ],
            }
        )

    with pytest.raises(promotion_smoke.SmokeCheckError, match="owned_by=Meta"):
        promotion_smoke.validate_models(
            {
                "object": "list",
                "data": [
                    {"id": "llama-3.1-8b-instruct", "owned_by": "token.place"},
                ],
            }
        )


def test_validate_diagnostics_requires_consistent_api_v1_live_node_count() -> None:
    promotion_smoke.validate_diagnostics(
        {
            "total_registered_compute_nodes": 2,
            "registered_compute_nodes": [{}, {}],
            "total_api_v1_registered_compute_nodes": 1,
            "api_v1_registered_compute_nodes": [{}],
        }
    )

    with pytest.raises(promotion_smoke.SmokeCheckError, match="must match listed nodes"):
        promotion_smoke.validate_diagnostics(
            {
                "total_registered_compute_nodes": 2,
                "total_api_v1_registered_compute_nodes": 2,
                "api_v1_registered_compute_nodes": [{}],
            }
        )


def test_run_smoke_checks_uses_expected_json_endpoints_without_network() -> None:
    config = promotion_smoke.SmokeConfig(base_url="https://staging.token.place/", environment="staging")
    responses = {
        "https://staging.token.place/livez": {"status": "alive"},
        "https://staging.token.place/healthz": {"status": "ok"},
        "https://staging.token.place/relay/diagnostics": {
            "total_registered_compute_nodes": 2,
            "total_api_v1_registered_compute_nodes": 2,
            "api_v1_registered_compute_nodes": [{}, {}],
        },
        "https://staging.token.place/api/v1/models": {
            "object": "list",
            "data": [{"id": "llama-3.1-8b-instruct", "owned_by": "Meta"}],
        },
    }
    called: list[str] = []

    def fake_fetcher(url: str, timeout_seconds: float):
        assert timeout_seconds == promotion_smoke.DEFAULT_TIMEOUT_SECONDS
        called.append(url)
        return responses[url]

    successes = promotion_smoke.run_smoke_checks(config, fetcher=fake_fetcher)

    assert called == list(responses)
    assert successes == [
        "/livez ok",
        "/healthz ok",
        "/relay/diagnostics ok",
        "/api/v1/models ok",
    ]
