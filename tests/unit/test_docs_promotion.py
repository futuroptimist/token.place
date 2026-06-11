from __future__ import annotations

from pathlib import Path

PROMOTION_DOC = Path("docs/PRODUCTION_PROMOTION.md")


def test_production_promotion_checklist_covers_0_1_0_guardrails() -> None:
    text = PROMOTION_DOC.read_text(encoding="utf-8")

    required_phrases = [
        "green CI checks, including Linux and macOS",
        "run_all_tests.sh",
        "staging deployment image, Helm chart, and release artifact",
        "Windows and macOS desktop release candidates install successfully",
        "GET /livez",
        "GET /healthz",
        "GET /relay/diagnostics",
        "total_registered_compute_nodes",
        "total_api_v1_registered_compute_nodes",
        "GET /api/v1/models",
        "exactly one public model: `llama-3.1-8b-instruct`",
        "landing dropdown has exactly one model option",
        "does not show `owned by token.place`",
        "two new browser clients round-robin",
        "remains sticky to its selected server across multiple turns",
        "automatic failover to another available compute node without losing visible chat history",
        "No full public key is rendered in the DOM",
        "no `/api/v2` calls",
        "no `/api/v1/chat/completions` calls",
        "production secrets",
        "relay registration tokens",
        "rate-limit storage",
        "rollback path",
        "ciphertext only plus safe routing metadata",
    ]

    missing = [phrase for phrase in required_phrases if phrase not in text]
    assert not missing, "Promotion checklist missing required guardrails: " + ", ".join(missing)


def test_promotion_smoke_helper_is_documented_as_opt_in_and_offline_by_default() -> None:
    text = PROMOTION_DOC.read_text(encoding="utf-8")

    assert "scripts/promotion_smoke.py" in text
    assert "RUN_PROMOTION_SMOKE=1" in text
    assert "TOKENPLACE_SMOKE_BASE_URL=https://staging.token.place" in text
    assert "TOKENPLACE_SMOKE_ALLOW_PROD=1" in text
    assert "exits without making network requests" in text
    assert "does not send chat" in text
