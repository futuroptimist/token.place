from __future__ import annotations

from pathlib import Path

PROMOTION_DOC = Path("docs/PRODUCTION_PROMOTION.md")
TESTING_DOC = Path("docs/TESTING.md")


def test_production_promotion_checklist_covers_0_1_0_risks() -> None:
    text = PROMOTION_DOC.read_text(encoding="utf-8")

    required_phrases = [
        "Linux and macOS `./run_all_tests.sh` PR checks",
        "staging deployment image, chart, and release artifact",
        "Desktop releases for Windows and macOS install successfully and register as compute nodes",
        "`GET /livez` returns healthy liveness status",
        "`GET /healthz` returns healthy readiness status",
        "`GET /relay/diagnostics` reports the live node count accurately",
        "`GET /api/v1/models` returns exactly one public model: `llama-3.1-8b-instruct`",
        "landing-page model dropdown has exactly one model",
        "does not show `owned by token.place`",
        "Two compute nodes round-robin across new browser clients",
        "remains sticky to its selected server across multiple turns",
        "automatic failover to another available node without losing chat history",
        "No full public key is rendered in the DOM",
        "no `/api/v2` calls",
        "no direct `/api/v1/chat/completions` calls",
        "Production secrets, relay registration tokens",
        "Rate-limit storage and production environment settings",
        "Rollback path is documented",
    ]

    missing = [phrase for phrase in required_phrases if phrase not in text]
    assert not missing


def test_promotion_smoke_helper_is_documented_as_opt_in_and_offline_safe() -> None:
    combined = PROMOTION_DOC.read_text(encoding="utf-8") + TESTING_DOC.read_text(
        encoding="utf-8"
    )

    assert "scripts/promotion_smoke.py" in combined
    assert "RUN_PROMOTION_SMOKE=1" in combined
    assert "TOKENPLACE_SMOKE_BASE_URL" in combined
    assert "TOKENPLACE_SMOKE_ALLOW_PROD=1" in combined
    assert "does not contact live services unless" in combined
