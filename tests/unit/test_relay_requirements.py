"""Guardrail: relay-only installs must include packages imported by relay.py."""

from __future__ import annotations

from pathlib import Path


def _relay_requirements_text() -> str:
    path = Path(__file__).resolve().parents[2] / "config" / "requirements_relay.txt"
    return path.read_text(encoding="utf-8")


def test_relay_requirements_include_api_stack_dependencies() -> None:
    """Relay imports api/, encrypt.py, and utils/; requirements must list their runtime deps."""
    text = _relay_requirements_text().lower()
    required = (
        "cryptography",
        "python-dotenv",
        "psutil",
        "openai",
        "httpx",
        "jsonschema",
        "pyyaml",
        "pillow",
    )
    missing = [name for name in required if name not in text]
    assert not missing, f"config/requirements_relay.txt is missing: {', '.join(missing)}"
