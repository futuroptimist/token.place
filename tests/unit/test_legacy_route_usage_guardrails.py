from pathlib import Path

LEGACY_ROUTES = ("/sink", "/faucet", "/source", "/retrieve", "/next_server")


def test_active_production_paths_do_not_reference_legacy_relay_routes():
    root = Path(__file__).resolve().parents[2]
    targets = [
        root / "api" / "v1" / "compute_provider.py",
        root / "static" / "chat.js",
        root / "desktop" / "src" / "services" / "desktopBridgeClient.ts",
        root / "desktop" / "src" / "services" / "desktopApiClient.ts",
    ]

    violations: list[str] = []
    for path in targets:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for route in LEGACY_ROUTES:
            if route in text:
                violations.append(f"{path.relative_to(root)} uses deprecated route {route}")

    assert not violations, "\n".join(violations)
