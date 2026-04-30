from pathlib import Path

LEGACY_ENDPOINTS = ("/sink", "/faucet", "/source", "/retrieve", "/next_server")


def test_active_api_v1_paths_do_not_use_legacy_relay_endpoints():
    root = Path(__file__).resolve().parents[2]
    targets = [
        root / "api" / "v1" / "compute_provider.py",
        root / "static" / "chat.js",
        root / "desktop" / "compute_node_bridge.py",
        root / "relay.py",
    ]

    allowed_relay_lines = {
        "@app.route('/sink', methods=['POST'])",
        "@app.route('/faucet', methods=['POST'])",
        "@app.route('/source', methods=['POST'])",
        "@app.route('/retrieve', methods=['POST'])",
        "@app.route('/next_server', methods=['GET'])",
        "Legacy relay endpoint deprecated.",
    }

    violations: list[str] = []
    for path in targets:
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not any(endpoint in line for endpoint in LEGACY_ENDPOINTS):
                continue
            if path.name == "relay.py" and any(token in line for token in allowed_relay_lines):
                continue
            violations.append(f"{path.relative_to(root)}:{line_no}:{line.strip()}")

    assert not violations, "Legacy relay endpoint usage detected in active paths:\n" + "\n".join(violations)
