from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_desktop_bundle_resources_do_not_include_relay_entrypoints() -> None:
    conf = (REPO_ROOT / "desktop-tauri" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    assert "relay.py" not in conf
    assert "5010" not in conf


def test_desktop_startup_sources_do_not_spawn_relay() -> None:
    startup_sources = [
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "src" / "main.rs",
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "src" / "compute_node.rs",
        REPO_ROOT / "desktop-tauri" / "src" / "App.tsx",
    ]
    markers = ["relay.py", "localhost:5010", "127.0.0.1:5010", "RELAY_PORT"]
    for path in startup_sources:
        text = path.read_text(encoding="utf-8")
        for marker in markers:
            assert marker not in text, f"{path} contains unexpected desktop relay marker: {marker}"
