from pathlib import Path

LEGACY_ENDPOINTS = ("/sink", "/faucet", "/source", "/retrieve", "/next_server")


def test_active_production_paths_do_not_use_legacy_relay_endpoints():
    root = Path(__file__).resolve().parents[2]
    active_files = [
        root / "api" / "v1" / "compute_provider.py",
        root / "static" / "chat.js",
        root / "desktop" / "src-tauri" / "src" / "compute_node_bridge.rs",
        root / "relay.py",
    ]

    violations = []
    relay_allowed_lines = (
        "@app.route('/sink'",
        "@app.route('/faucet'",
        "@app.route('/source'",
        "@app.route('/retrieve'",
        "@app.route('/next_server'",
        "Legacy relay endpoint deprecated",
    )

    for file_path in active_files:
        if not file_path.exists():
            continue
        for idx, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(endpoint in line for endpoint in LEGACY_ENDPOINTS):
                if file_path.name == "relay.py" and any(token in line for token in relay_allowed_lines):
                    continue
                violations.append(f"{file_path.relative_to(root)}:{idx}:{line.strip()}")

    assert not violations, "Active production code references legacy relay endpoints:\n" + "\n".join(violations)
