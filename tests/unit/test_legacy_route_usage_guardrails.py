from pathlib import Path
import re

LEGACY_ROUTES = ("/sink", "/faucet", "/source", "/retrieve", "/next_server")


def test_active_production_paths_do_not_reference_legacy_relay_routes():
    root = Path(__file__).resolve().parents[2]
    targets = [
        root / "api" / "v1" / "compute_provider.py",
        root / "client.py",
        root / "utils" / "crypto_helpers.py",
        root / "utils" / "compute_node_runtime.py",
        root / "static" / "chat.js",
        root / "desktop" / "src" / "services" / "desktopBridgeClient.ts",
        root / "desktop" / "src" / "services" / "desktopApiClient.ts",
        root / "desktop-tauri" / "src" / "App.tsx",
        root / "desktop-tauri" / "src-tauri" / "src" / "forward.rs",
    ]

    violations: list[str] = []
    for path in targets:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for route in LEGACY_ROUTES:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(route)}(?![A-Za-z0-9_])")
            if pattern.search(text):
                violations.append(f"{path.relative_to(root)} uses deprecated route {route}")

    assert not violations, "\n".join(violations)


def test_api_v1_relay_client_paths_do_not_reference_legacy_relay_routes():
    root = Path(__file__).resolve().parents[2]
    path = root / "utils" / "networking" / "relay_client.py"
    text = path.read_text(encoding="utf-8")
    active_sections = [
        ("def poll_api_v1_encrypted_work", "def _api_v1_response_relay_url"),
        ("def _api_v1_response_relay_url", "def process_client_request"),
        ("if api_v1_request_payload is not None:", "chat_history = _extract_chat_history"),
        ("def poll_api_v1_encrypted_work_continuously", "def poll_relay_continuously"),
    ]

    violations: list[str] = []
    for start_marker, end_marker in active_sections:
        start = text.index(start_marker)
        end = text.index(end_marker, start + len(start_marker))
        section = text[start:end]
        for route in LEGACY_ROUTES:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(route)}(?![A-Za-z0-9_])")
            if pattern.search(section):
                violations.append(f"{path.relative_to(root)} active API v1 path uses {route}")

    assert not violations, "\n".join(violations)
