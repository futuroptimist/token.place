from __future__ import annotations

from pathlib import Path

import pytest


MARKER_TO_PHRASE = {
    "unit": "Unit Tests",
    "integration": "Integration Tests",
    "api": "API Tests",
    "crypto": "Crypto Tests",
    "js": "JavaScript Tests",
    "browser": "Browser Tests",
    "slow": "Slow Tests",
    "visual": "Visual Verification Tests",
    "benchmark": "Benchmark Tests",
    "failure": "Failure Recovery Tests",
    "e2e": "End-to-End (E2E) Tests",
    "parametrize": "Parameterized Tests",
    "real_llm": "Real LLM Tests",
    "security": "Security Tests",
}


def _load_pytest_markers(pytest_ini: Path) -> list[str]:
    lines = pytest_ini.read_text(encoding="utf-8").splitlines()
    markers: list[str] = []
    collecting = False

    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("markers"):
            collecting = True
            continue

        if collecting:
            if not stripped.startswith("    ") or not stripped.strip():
                break
            marker_name = stripped.strip().split(":", 1)[0]
            markers.append(marker_name)

    return markers


@pytest.mark.unit
def test_testing_doc_covers_all_markers() -> None:
    markers = _load_pytest_markers(Path("pytest.ini"))
    guide_text = Path("docs/TESTING.md").read_text(encoding="utf-8")

    undocumented = [
        marker
        for marker in markers
        if MARKER_TO_PHRASE[marker].lower() not in guide_text.lower()
    ]

    assert not undocumented, (
        "docs/TESTING.md is missing sections for the following pytest markers: "
        + ", ".join(undocumented)
    )
