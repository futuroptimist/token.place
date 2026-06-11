from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path


def _collect_pytest_markers() -> list[str]:
    config = ConfigParser()
    config.read("pytest.ini")
    markers_raw = config.get("pytest", "markers", fallback="")
    markers: list[str] = []
    for line in markers_raw.splitlines():
        name, _, _ = line.partition(":")
        name = name.strip()
        if name:
            markers.append(name)
    return markers


def test_testing_guide_mentions_all_markers() -> None:
    markers = _collect_pytest_markers()
    testing_doc = Path("docs/TESTING.md").read_text(encoding="utf-8")
    missing = [marker for marker in markers if f"`{marker}`" not in testing_doc]
    assert not missing, (
        "docs/TESTING.md should document each pytest marker. Missing: " + ", ".join(missing)
    )


def test_production_promotion_checklist_covers_0_1_0_smoke_risks() -> None:
    promotion_doc = Path("docs/PRODUCTION_PROMOTION.md").read_text(encoding="utf-8")

    required_phrases = [
        "Linux and macOS",
        "./run_all_tests.sh",
        "Windows and macOS",
        "/livez",
        "/healthz",
        "/relay/diagnostics",
        "live compute-node count",
        "/api/v1/models",
        "exactly one public model",
        "llama-3.1-8b-instruct",
        "landing-page model dropdown has exactly one model",
        "owned by token.place",
        "round-robin",
        "sticky",
        "automatic failover",
        "without losing the visible chat history",
        "no full public key is rendered in the DOM",
        "no `/api/v2` calls",
        "no direct `/api/v1/chat/completions` calls",
        "production secrets and relay registration tokens",
        "rate-limit storage",
        "rollback path",
        "ciphertext-only plus safe routing metadata",
    ]
    missing = [phrase for phrase in required_phrases if phrase not in promotion_doc]
    assert not missing, "docs/PRODUCTION_PROMOTION.md is missing: " + ", ".join(missing)


def test_testing_guide_links_safe_promotion_smoke_helper() -> None:
    testing_doc = Path("docs/TESTING.md").read_text(encoding="utf-8")

    assert "PRODUCTION_PROMOTION.md" in testing_doc
    assert "RUN_PROMOTION_SMOKE=1" in testing_doc
    assert "TOKENPLACE_SMOKE_BASE_URL" in testing_doc
    assert "python scripts/promotion_smoke.py" in testing_doc
    assert "remain offline" in testing_doc
