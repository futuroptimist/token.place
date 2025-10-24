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
