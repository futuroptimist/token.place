"""Guardrail: relay-only installs must include packages imported by relay.py."""

from __future__ import annotations

from pathlib import Path


def _relay_requirement_names() -> set[str]:
    path = Path(__file__).resolve().parents[2] / "config" / "requirements_relay.txt"
    names: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        candidate = line
        for separator in ("==", ">=", "<=", "~=", "!=", "<", ">", ";"):
            if separator in candidate:
                candidate = candidate.split(separator, 1)[0]
                break

        normalized = candidate.strip().lower().replace("_", "-")
        if normalized:
            names.add(normalized)

    return names


def test_relay_requirements_include_api_stack_dependencies() -> None:
    """Relay imports api/, encrypt.py, and utils/; requirements must list runtime deps."""
    names = _relay_requirement_names()
    required = (
        "cryptography",
        "python-dotenv",
        "psutil",
        "jsonschema",
        "pyyaml",
        "pillow",
    )
    missing = [name for name in required if name not in names]
    assert not missing, f"config/requirements_relay.txt is missing: {', '.join(missing)}"
