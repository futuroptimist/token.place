"""Desktop guardrails: tauri app must not bundle or autostart relay.py."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_tauri_bundle_resources_exclude_relay_py() -> None:
    config = json.loads((REPO_ROOT / "desktop-tauri" / "src-tauri" / "tauri.conf.json").read_text())
    resources = config["bundle"]["resources"]
    assert all("relay.py" not in str(entry) for entry in resources)


def test_desktop_rust_sources_do_not_include_relay_spawn_patterns() -> None:
    forbidden = ("Command::new(\"relay.py\"", "python relay.py", "localhost:5010", "127.0.0.1:5010")
    src_root = REPO_ROOT / "desktop-tauri" / "src-tauri" / "src"
    for path in src_root.glob("*.rs"):
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            assert pattern not in text, f"desktop source contains forbidden relay pattern {pattern}: {path}"
