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


@pytest.mark.parametrize(
    ("invalid_base_url", "match"),
    [
        ("   ", "must not be empty"),
        ("ftp://staging.token.place", "must use http:// or https://"),
        ("https:///", "must include a host"),
    ],
)
def test_normalize_base_url_rejects_invalid_roots(
    invalid_base_url: str, match: str
) -> None:
    with pytest.raises(promotion_smoke.SmokeConfigError, match=match):
        promotion_smoke.normalize_base_url(invalid_base_url)


@pytest.mark.parametrize(
    ("validator", "payload", "match"),
    [
        (promotion_smoke.validate_livez, {"status": "ok"}, "status=alive"),
        (promotion_smoke.validate_healthz, {"status": "degraded"}, "status=ok"),
        (promotion_smoke.validate_diagnostics, [], "JSON object"),
        (
            promotion_smoke.validate_diagnostics,
            {
                "total_registered_compute_nodes": -1,
                "registered_compute_nodes": [],
                "total_api_v1_registered_compute_nodes": 0,
                "api_v1_registered_compute_nodes": [],
            },
            "non-negative integer",
        ),
        (promotion_smoke.validate_models, {"object": "model"}, "list object"),
        (
            promotion_smoke.validate_models,
            {"object": "list", "data": {}},
            "data must be a list",
        ),
    ],
)
def test_endpoint_validators_reject_malformed_payloads(
    validator, payload, match: str
) -> None:
    with pytest.raises(promotion_smoke.SmokeCheckError, match=match):
        validator(payload)


class _FakeResponse:
    def __init__(self, *, status: int = 200, body: str = "{}") -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body.encode("utf-8")


def test_fetch_json_returns_decoded_payload_without_live_network(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout: float):
        captured["url"] = request.full_url
        captured["accept"] = request.headers["Accept"]
        captured["user_agent"] = request.headers["User-agent"]
        captured["timeout"] = timeout
        return _FakeResponse(body='{"status": "alive"}')

    monkeypatch.setattr(promotion_smoke, "urlopen", fake_urlopen)

    payload = promotion_smoke.fetch_json("https://staging.token.place/livez", 3.5)

    assert payload == {"status": "alive"}
    assert captured == {
        "url": "https://staging.token.place/livez",
        "accept": "application/json",
        "user_agent": "tokenplace-promotion-smoke/1",
        "timeout": 3.5,
    }


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (_FakeResponse(status=503, body='{"status": "down"}'), "returned HTTP 503"),
        (_FakeResponse(body="not json"), "did not return valid JSON"),
    ],
)
def test_fetch_json_rejects_bad_responses_without_live_network(
    monkeypatch, response: _FakeResponse, match: str
) -> None:
    def fake_urlopen(request, timeout: float):
        return response

    monkeypatch.setattr(promotion_smoke, "urlopen", fake_urlopen)

    with pytest.raises(promotion_smoke.SmokeCheckError, match=match):
        promotion_smoke.fetch_json("https://staging.token.place/livez")


def test_parse_args_accepts_timeout() -> None:
    args = promotion_smoke.parse_args(["--timeout", "2.25"])

    assert args.timeout == 2.25


def test_main_reports_config_errors_without_live_network(monkeypatch, capsys) -> None:
    monkeypatch.delenv("RUN_PROMOTION_SMOKE", raising=False)

    exit_code = promotion_smoke.main([])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "promotion smoke failed: Set RUN_PROMOTION_SMOKE=1" in captured.err
    assert captured.out == ""


def test_main_prints_successes_with_injected_offline_runner(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("RUN_PROMOTION_SMOKE", "1")
    monkeypatch.setenv("TOKENPLACE_SMOKE_BASE_URL", "https://staging.token.place")

    def fake_run_smoke_checks(config):
        assert config.base_url == "https://staging.token.place/"
        assert config.environment == "staging"
        assert config.timeout_seconds == 1.5
        return ["/livez ok", "/healthz ok"]

    monkeypatch.setattr(promotion_smoke, "run_smoke_checks", fake_run_smoke_checks)

    exit_code = promotion_smoke.main(["--timeout", "1.5"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == "/livez ok\n/healthz ok\n"
    assert captured.err == ""


def test_config_from_env_accepts_optional_release_metadata_expectations() -> None:
    config = promotion_smoke.config_from_env(
        {
            "RUN_PROMOTION_SMOKE": "1",
            "TOKENPLACE_SMOKE_BASE_URL": "https://staging.token.place",
            "TOKENPLACE_SMOKE_EXPECT_VERSION": "0.1.1",
            "TOKENPLACE_SMOKE_EXPECT_ENV": "staging",
        }
    )

    assert config.expected_version == "0.1.1"
    assert config.expected_environment == "staging"


def test_run_smoke_checks_optionally_validates_release_metadata_offline() -> None:
    config = promotion_smoke.SmokeConfig(
        base_url="https://staging.token.place/",
        environment="staging",
        expected_version="0.1.1",
        expected_environment="staging",
    )
    responses = {
        "https://staging.token.place/livez": {"status": "alive"},
        "https://staging.token.place/healthz": {"status": "ok"},
        "https://staging.token.place/relay/diagnostics": {
            "total_registered_compute_nodes": 1,
            "registered_compute_nodes": [{}],
            "total_api_v1_registered_compute_nodes": 1,
            "api_v1_registered_compute_nodes": [{}],
        },
        "https://staging.token.place/api/v1/models": {
            "object": "list",
            "data": [{"id": "llama-3.1-8b-instruct", "owned_by": "Meta"}],
        },
        "https://staging.token.place/api/v1/version": {
            "environment": "staging",
            "version": "0.1.1",
            "label": "staging 0.1.1",
        },
    }
    called: list[str] = []

    def fake_fetcher(url: str, timeout_seconds: float):
        called.append(url)
        return responses[url]

    successes = promotion_smoke.run_smoke_checks(config, fetcher=fake_fetcher)

    assert called[-1] == "https://staging.token.place/api/v1/version"
    assert successes[-1] == "/api/v1/version ok"


def test_release_metadata_validator_rejects_secret_shaped_extra_keys() -> None:
    with pytest.raises(promotion_smoke.SmokeCheckError, match="unexpected keys"):
        promotion_smoke.validate_release_metadata(
            {
                "environment": "prod",
                "version": "0.1.1",
                "label": "prod 0.1.1",
                "database_password": "secret",
            },
            expected_version="0.1.1",
            expected_environment="prod",
        )


def test_release_metadata_validator_checks_expected_version_and_environment() -> None:
    payload = {"environment": "staging", "version": "0.1.1", "label": "staging 0.1.1"}

    promotion_smoke.validate_release_metadata(
        payload,
        expected_version="0.1.1",
        expected_environment="staging",
    )

    with pytest.raises(promotion_smoke.SmokeCheckError, match="expected '0.1.0'"):
        promotion_smoke.validate_release_metadata(
            payload,
            expected_version="0.1.0",
            expected_environment="staging",
        )
    with pytest.raises(promotion_smoke.SmokeCheckError, match="expected 'prod'"):
        promotion_smoke.validate_release_metadata(
            payload,
            expected_version="0.1.1",
            expected_environment="prod",
        )


def test_release_metadata_validator_rejects_inconsistent_label() -> None:
    with pytest.raises(promotion_smoke.SmokeCheckError, match="label='staging 0.1.0'"):
        promotion_smoke.validate_release_metadata(
            {"environment": "staging", "version": "0.1.1", "label": "staging 0.1.0"},
            expected_version="0.1.1",
            expected_environment="staging",
        )


def test_release_metadata_validator_uses_ref_for_dev_label() -> None:
    promotion_smoke.validate_release_metadata(
        {
            "environment": "dev",
            "version": "dev",
            "label": "dev abc123def456",
            "ref": "abc123def456",
        },
        expected_version="dev",
        expected_environment="dev",
    )

    with pytest.raises(promotion_smoke.SmokeCheckError, match="label='dev dev'"):
        promotion_smoke.validate_release_metadata(
            {
                "environment": "dev",
                "version": "dev",
                "label": "dev dev",
                "ref": "abc123def456",
            },
            expected_version="dev",
            expected_environment="dev",
        )
