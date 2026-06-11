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
    with pytest.raises(
        promotion_smoke.SmokeConfigError, match="TOKENPLACE_SMOKE_ALLOW_PROD"
    ):
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


def test_validate_diagnostics_accepts_matching_live_node_counts() -> None:
    promotion_smoke.validate_diagnostics(
        {
            "total_registered_compute_nodes": 0,
            "registered_compute_nodes": [],
            "total_api_v1_registered_compute_nodes": 0,
            "api_v1_registered_compute_nodes": [],
        }
    )
    promotion_smoke.validate_diagnostics(
        {
            "total_registered_compute_nodes": 2,
            "registered_compute_nodes": [{}, {}],
            "total_api_v1_registered_compute_nodes": 1,
            "api_v1_registered_compute_nodes": [{}],
        }
    )


def test_validate_diagnostics_requires_registered_compute_nodes_list() -> None:
    with pytest.raises(
        promotion_smoke.SmokeCheckError, match="registered_compute_nodes must be a list"
    ):
        promotion_smoke.validate_diagnostics(
            {
                "total_registered_compute_nodes": 0,
                "total_api_v1_registered_compute_nodes": 0,
                "api_v1_registered_compute_nodes": [],
            }
        )


def test_validate_diagnostics_requires_api_v1_registered_compute_nodes_list() -> None:
    with pytest.raises(
        promotion_smoke.SmokeCheckError,
        match="api_v1_registered_compute_nodes must be a list",
    ):
        promotion_smoke.validate_diagnostics(
            {
                "total_registered_compute_nodes": 0,
                "registered_compute_nodes": [],
                "total_api_v1_registered_compute_nodes": 0,
            }
        )


def test_validate_diagnostics_rejects_registered_total_mismatch() -> None:
    with pytest.raises(
        promotion_smoke.SmokeCheckError,
        match="total_registered_compute_nodes must match registered_compute_nodes",
    ):
        promotion_smoke.validate_diagnostics(
            {
                "total_registered_compute_nodes": 2,
                "registered_compute_nodes": [{}],
                "total_api_v1_registered_compute_nodes": 1,
                "api_v1_registered_compute_nodes": [{}],
            }
        )


def test_validate_diagnostics_rejects_api_v1_registered_total_mismatch() -> None:
    with pytest.raises(
        promotion_smoke.SmokeCheckError,
        match=(
            "total_api_v1_registered_compute_nodes "
            "must match api_v1_registered_compute_nodes"
        ),
    ):
        promotion_smoke.validate_diagnostics(
            {
                "total_registered_compute_nodes": 2,
                "registered_compute_nodes": [{}, {}],
                "total_api_v1_registered_compute_nodes": 2,
                "api_v1_registered_compute_nodes": [{}],
            }
        )


def test_run_smoke_checks_uses_expected_json_endpoints_without_network() -> None:
    config = promotion_smoke.SmokeConfig(
        base_url="https://staging.token.place/", environment="staging"
    )
    responses = {
        "https://staging.token.place/livez": {"status": "alive"},
        "https://staging.token.place/healthz": {"status": "ok"},
        "https://staging.token.place/relay/diagnostics": {
            "total_registered_compute_nodes": 2,
            "registered_compute_nodes": [{}, {}],
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


def test_config_from_env_rejects_pathful_base_url() -> None:
    with pytest.raises(
        promotion_smoke.SmokeConfigError, match="must not include a path"
    ):
        promotion_smoke.config_from_env(
            {
                "RUN_PROMOTION_SMOKE": "1",
                "TOKENPLACE_SMOKE_BASE_URL": "https://staging.token.place/api",
            }
        )


def test_config_from_env_rejects_query_or_fragment_base_url() -> None:
    with pytest.raises(
        promotion_smoke.SmokeConfigError, match="params, query, or fragment"
    ):
        promotion_smoke.config_from_env(
            {
                "RUN_PROMOTION_SMOKE": "1",
                "TOKENPLACE_SMOKE_BASE_URL": "https://staging.token.place/?debug=1",
            }
        )


def test_config_from_env_refuses_production_like_subdomain_without_acknowledgement() -> (
    None
):
    with pytest.raises(
        promotion_smoke.SmokeConfigError, match="TOKENPLACE_SMOKE_ALLOW_PROD"
    ):
        promotion_smoke.config_from_env(
            {
                "RUN_PROMOTION_SMOKE": "1",
                "TOKENPLACE_SMOKE_BASE_URL": "https://prod.token.place",
            }
        )


def test_config_from_env_refuses_production_like_cname_without_acknowledgement() -> (
    None
):
    with pytest.raises(
        promotion_smoke.SmokeConfigError, match="TOKENPLACE_SMOKE_ALLOW_PROD"
    ):
        promotion_smoke.config_from_env(
            {
                "RUN_PROMOTION_SMOKE": "1",
                "TOKENPLACE_SMOKE_BASE_URL": "https://relay.prod.example.com",
            }
        )


def test_endpoint_url_uses_root_relative_endpoint() -> None:
    assert (
        promotion_smoke.endpoint_url("https://staging.token.place/", "/livez")
        == "https://staging.token.place/livez"
    )


def test_validate_healthz_rejects_degraded_details_even_when_status_ok() -> None:
    with pytest.raises(promotion_smoke.SmokeCheckError, match="degraded details"):
        promotion_smoke.validate_healthz(
            {"status": "ok", "details": {"knownServers": "empty"}}
        )


def test_validate_models_rejects_non_object_entries() -> None:
    with pytest.raises(
        promotion_smoke.SmokeCheckError, match="entries must be objects"
    ):
        promotion_smoke.validate_models(
            {
                "object": "list",
                "data": ["oops", {"id": "llama-3.1-8b-instruct", "owned_by": "Meta"}],
            }
        )


def test_run_smoke_checks_rejects_unknown_endpoint_without_network() -> None:
    config = promotion_smoke.SmokeConfig(
        base_url="https://staging.token.place/", environment="staging"
    )

    def fail_fetcher(
        url: str, timeout_seconds: float
    ):  # pragma: no cover - must not run
        raise AssertionError(
            f"unexpected fetch of {url} with timeout {timeout_seconds}"
        )

    with pytest.raises(
        promotion_smoke.SmokeConfigError, match="Unsupported smoke endpoint"
    ):
        promotion_smoke.run_smoke_checks(
            config, fetcher=fail_fetcher, endpoints=("/unknown",)
        )
