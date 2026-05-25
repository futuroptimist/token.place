"""Guards that desktop-tauri runtime does not bundle or autostart relay.py."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_tauri_resources_do_not_bundle_relay_py() -> None:
    tauri_conf = REPO_ROOT / "desktop-tauri" / "src-tauri" / "tauri.conf.json"
    text = tauri_conf.read_text(encoding="utf-8")
    assert "relay.py" not in text


def test_desktop_runtime_sources_do_not_spawn_relay_py() -> None:
    desktop_sources = [
        REPO_ROOT / "desktop-tauri" / "src" / "App.tsx",
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "src" / "main.rs",
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "src" / "compute_node.rs",
        REPO_ROOT / "desktop-tauri" / "src-tauri" / "src" / "python_runtime.rs",
    ]
    for source in desktop_sources:
        content = source.read_text(encoding="utf-8")
        assert "relay.py" not in content
