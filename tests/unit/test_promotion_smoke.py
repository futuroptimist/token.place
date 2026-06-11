from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "promotion_smoke.py"
spec = importlib.util.spec_from_file_location("promotion_smoke", MODULE_PATH)
promotion_smoke = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = promotion_smoke
spec.loader.exec_module(promotion_smoke)


def test_normalize_base_url_accepts_http_and_strips_path() -> None:
    assert promotion_smoke.normalize_base_url(" https://staging.example.com/relay ") == "https://staging.example.com"
    assert promotion_smoke.endpoint_url("https://staging.example.com", "/api/v1/models") == "https://staging.example.com/api/v1/models"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "staging.example.com",
        "ftp://staging.example.com",
        "https://user:secret@staging.example.com",
        "https://staging.example.com?token=secret",
        "https://staging.example.com/#fragment",
    ],
)
def test_normalize_base_url_rejects_unsafe_or_ambiguous_targets(url: str) -> None:
    with pytest.raises(promotion_smoke.SmokeError):
        promotion_smoke.normalize_base_url(url)


def test_diagnostics_payload_requires_count_to_match_node_list() -> None:
    promotion_smoke.assert_diagnostics_payload(
        {
            "registered_compute_nodes": [{"server_public_key": "short-safe-label"}],
            "total_registered_compute_nodes": 1,
            "api_v1_registered_compute_nodes": [{"server_public_key": "short-safe-label"}],
            "total_api_v1_registered_compute_nodes": 1,
        }
    )

    with pytest.raises(promotion_smoke.SmokeError, match="does not match"):
        promotion_smoke.assert_diagnostics_payload(
            {"registered_compute_nodes": [], "total_registered_compute_nodes": 1}
        )


def test_models_payload_freezes_single_public_launch_model() -> None:
    promotion_smoke.assert_models_payload(
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

    with pytest.raises(promotion_smoke.SmokeError, match="exactly one public model"):
        promotion_smoke.assert_models_payload(
            {
                "object": "list",
                "data": [
                    {"id": "llama-3.1-8b-instruct", "object": "model"},
                    {"id": "llama-3.1-8b-instruct:alignment", "object": "model"},
                ],
            }
        )

    with pytest.raises(promotion_smoke.SmokeError, match="owned by token.place"):
        promotion_smoke.assert_models_payload(
            {
                "object": "list",
                "data": [
                    {
                        "id": "llama-3.1-8b-instruct",
                        "object": "model",
                        "owned_by": "owned by token.place",
                    }
                ],
            }
        )


def test_main_skips_without_explicit_enablement(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("RUN_PROMOTION_SMOKE", raising=False)
    monkeypatch.setenv("TOKENPLACE_SMOKE_BASE_URL", "https://staging.example.com")

    assert promotion_smoke.main([]) == 0
    assert "Skipping promotion smoke checks" in capsys.readouterr().out
