from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path("scripts/promotion_smoke.py")
spec = importlib.util.spec_from_file_location("promotion_smoke", SCRIPT)
promotion_smoke = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = promotion_smoke
spec.loader.exec_module(promotion_smoke)


def test_build_config_is_disabled_without_opt_in() -> None:
    config = promotion_smoke.build_config({})

    assert config.enabled is False
    assert promotion_smoke.run_smoke(config) == {
        "status": "skipped",
        "reason": "RUN_PROMOTION_SMOKE is not enabled",
    }


def test_production_requires_explicit_allow_flag() -> None:
    with pytest.raises(promotion_smoke.PromotionSmokeError, match="ALLOW_PROD"):
        promotion_smoke.build_config(
            {
                "RUN_PROMOTION_SMOKE": "1",
                "TOKENPLACE_SMOKE_ENV": "prod",
                "TOKENPLACE_SMOKE_BASE_URL": "https://token.place",
            }
        )


def test_normalize_base_url_accepts_absolute_http_urls_without_query() -> None:
    assert (
        promotion_smoke.normalize_base_url(" https://staging.token.place/ ")
        == "https://staging.token.place"
    )
    assert (
        promotion_smoke.endpoint_url("https://staging.token.place/relay/", "/livez")
        == "https://staging.token.place/relay/livez"
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "token.place",
        "ftp://token.place",
        "https://token.place?debug=1",
        "https://token.place/#x",
    ],
)
def test_normalize_base_url_rejects_unsafe_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(promotion_smoke.PromotionSmokeError):
        promotion_smoke.normalize_base_url(url)


def test_run_smoke_validates_expected_endpoint_payloads() -> None:
    config = promotion_smoke.build_config(
        {
            "RUN_PROMOTION_SMOKE": "1",
            "TOKENPLACE_SMOKE_ENV": "staging",
            "TOKENPLACE_SMOKE_BASE_URL": "https://staging.token.place/",
        }
    )
    payloads = {
        "https://staging.token.place/livez": {"status": "alive"},
        "https://staging.token.place/healthz": {
            "status": "ok",
            "knownServers": 2,
            "registeredServers": [
                {"server_public_key": "a"},
                {"server_public_key": "b"},
            ],
        },
        "https://staging.token.place/relay/diagnostics": {
            "registered_compute_nodes": [
                {"server_public_key": "a"},
                {"server_public_key": "b"},
            ],
            "total_registered_compute_nodes": 2,
            "api_v1_registered_compute_nodes": [
                {"server_public_key": "a"},
                {"server_public_key": "b"},
            ],
            "total_api_v1_registered_compute_nodes": 2,
        },
        "https://staging.token.place/api/v1/models": {
            "object": "list",
            "data": [
                {
                    "id": "llama-3.1-8b-instruct",
                    "object": "model",
                    "owned_by": "Meta",
                }
            ],
        },
    }

    assert promotion_smoke.run_smoke(config, fetcher=payloads.__getitem__) == {
        "/livez": "ok",
        "/healthz": "ok",
        "/relay/diagnostics": "ok",
        "/api/v1/models": "ok",
    }


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"object": "list", "data": []}, "exactly one"),
        (
            {
                "object": "list",
                "data": [{"id": "llama-3-8b-instruct", "owned_by": "Meta"}],
            },
            "llama-3.1-8b-instruct",
        ),
        (
            {
                "object": "list",
                "data": [{"id": "llama-3.1-8b-instruct", "owned_by": "token.place"}],
            },
            "ownership",
        ),
    ],
)
def test_validate_models_rejects_launch_contract_regressions(
    payload: dict, error: str
) -> None:
    with pytest.raises(promotion_smoke.PromotionSmokeError, match=error):
        promotion_smoke.validate_models(payload)


def test_validate_diagnostics_requires_counts_to_match_lists() -> None:
    with pytest.raises(promotion_smoke.PromotionSmokeError, match="total_api_v1"):
        promotion_smoke.validate_diagnostics(
            {
                "registered_compute_nodes": [],
                "total_registered_compute_nodes": 0,
                "api_v1_registered_compute_nodes": [{"server_public_key": "a"}],
                "total_api_v1_registered_compute_nodes": 0,
            }
        )
