"""Guardrails: desktop-tauri must never autostart relay.py or bind localhost:5010."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_CONFIG = REPO_ROOT / "desktop-tauri" / "src-tauri" / "tauri.conf.json"


def test_tauri_bundle_resources_exclude_relay_artifacts() -> None:
    config = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))
    resources = config.get("bundle", {}).get("resources", [])
    assert isinstance(resources, list)
    normalized = [str(item).lower() for item in resources]
    assert all("relay.py" not in item for item in normalized)
    assert all("relay" not in item or "requirements_relay" not in item for item in normalized)


def test_desktop_runtime_sources_do_not_reference_relay_autostart_or_5010() -> None:
    forbidden_exact = ("127.0.0.1:5010", "localhost:5010")
    desktop_files = [
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "src" / "main.rs",
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "src" / "compute_node.rs",
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "src" / "python_runtime.rs",
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "python" / "compute_node_bridge.py",
    ]
    for path in desktop_files:
        text = path.read_text(encoding="utf-8")
        for marker in forbidden_exact:
            assert marker not in text, f"{marker} unexpectedly present in {path}"
        assert "Command::new(\"relay.py\")" not in text, f"relay spawn marker present in {path}"
        assert "subprocess.Popen([sys.executable, \"relay.py\"" not in text, (
            f"relay spawn marker present in {path}"
        )
