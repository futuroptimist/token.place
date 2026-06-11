from __future__ import annotations

import json
import urllib.error

import pytest

from scripts import promotion_smoke


def test_normalize_base_url_defaults_https_and_strips_paths() -> None:
    assert promotion_smoke.normalize_base_url("staging.token.place/smoke?x=1") == "https://staging.token.place"
    assert promotion_smoke.normalize_base_url("https://staging.token.place/path/") == "https://staging.token.place"
    assert promotion_smoke.endpoint_url("https://staging.token.place", "/livez") == "https://staging.token.place/livez"


@pytest.mark.parametrize("raw_url", ["", "ftp://token.place", "https://"])
def test_normalize_base_url_rejects_unsafe_values(raw_url: str) -> None:
    with pytest.raises(promotion_smoke.SmokeCheckError):
        promotion_smoke.normalize_base_url(raw_url)


def test_production_url_requires_explicit_allowance() -> None:
    assert promotion_smoke.is_production_url("https://token.place")
    assert promotion_smoke.is_production_url("https://www.token.place")
    assert not promotion_smoke.is_production_url("https://staging.token.place")


def test_validate_models_requires_single_launch_model() -> None:
    promotion_smoke.validate_models(
        {
            "object": "list",
            "data": [
                {
                    "id": "llama-3.1-8b-instruct",
                    "object": "model",
                    "owned_by": "Meta",
                    "root": "llama-3.1-8b-instruct",
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"object": "list", "data": []},
        {
            "object": "list",
            "data": [
                {"id": "llama-3.1-8b-instruct", "owned_by": "Meta"},
                {"id": "llama-3.1-8b-instruct:alignment", "owned_by": "token.place"},
            ],
        },
        {"object": "list", "data": [{"id": "llama-3.1-8b-instruct", "owned_by": "token.place"}]},
        {"object": "list", "data": [{"id": "llama-3-8b-instruct", "owned_by": "Meta"}]},
    ],
)
def test_validate_models_rejects_promotion_contract_drift(payload: dict) -> None:
    with pytest.raises(promotion_smoke.SmokeCheckError):
        promotion_smoke.validate_models(payload)


def test_validate_diagnostics_requires_accurate_live_counts() -> None:
    promotion_smoke.validate_diagnostics(
        {
            "registered_compute_nodes": [{"server_public_key_fingerprint": "abc"}],
            "total_registered_compute_nodes": 1,
            "api_v1_registered_compute_nodes": [{"server_public_key_fingerprint": "abc"}],
            "total_api_v1_registered_compute_nodes": 1,
        }
    )

    with pytest.raises(promotion_smoke.SmokeCheckError):
        promotion_smoke.validate_diagnostics(
            {
                "registered_compute_nodes": [],
                "total_registered_compute_nodes": 1,
                "api_v1_registered_compute_nodes": [],
                "total_api_v1_registered_compute_nodes": 0,
            }
        )


def test_run_smoke_uses_only_json_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = {
        "/livez": {"status": "alive"},
        "/healthz": {"status": "ok", "registeredServers": []},
        "/relay/diagnostics": {
            "registered_compute_nodes": [],
            "total_registered_compute_nodes": 0,
            "api_v1_registered_compute_nodes": [],
            "total_api_v1_registered_compute_nodes": 0,
        },
        "/api/v1/models": {
            "object": "list",
            "data": [{"id": "llama-3.1-8b-instruct", "owned_by": "Meta"}],
        },
    }
    calls: list[str] = []

    def fake_fetch_json(base_url: str, endpoint: str, *, timeout: float = 10.0) -> dict:
        assert base_url == "https://staging.token.place"
        assert timeout == 3.0
        calls.append(endpoint)
        return payloads[endpoint]

    monkeypatch.setattr(promotion_smoke, "fetch_json", fake_fetch_json)

    results = promotion_smoke.run_smoke("https://staging.token.place/ignored", timeout=3.0)

    assert calls == list(promotion_smoke.JSON_ENDPOINTS)
    assert [result.endpoint for result in results] == list(promotion_smoke.JSON_ENDPOINTS)


def test_fetch_json_rejects_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 200
        headers = {"Content-Type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return b"ok"

    monkeypatch.setattr(promotion_smoke.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    with pytest.raises(promotion_smoke.SmokeCheckError):
        promotion_smoke.fetch_json("https://staging.token.place", "/livez")


def test_fetch_json_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return b"not json"

    monkeypatch.setattr(promotion_smoke.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    with pytest.raises(promotion_smoke.SmokeCheckError):
        promotion_smoke.fetch_json("https://staging.token.place", "/livez")


def test_fetch_json_returns_object(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self) -> int:
            return 200

        def read(self) -> bytes:
            return json.dumps({"status": "alive"}).encode("utf-8")

    monkeypatch.setattr(promotion_smoke.urllib.request, "urlopen", lambda request, timeout: FakeResponse())

    assert promotion_smoke.fetch_json("https://staging.token.place", "/livez") == {"status": "alive"}


def test_fetch_json_reports_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://staging.token.place/livez", 503, "down", {}, None)

    monkeypatch.setattr(promotion_smoke.urllib.request, "urlopen", fail)

    with pytest.raises(promotion_smoke.SmokeCheckError, match="HTTP 503"):
        promotion_smoke.fetch_json("https://staging.token.place", "/livez")
