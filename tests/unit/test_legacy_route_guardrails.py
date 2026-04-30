from pathlib import Path

LEGACY = ("/sink", "/faucet", "/source", "/retrieve", "/next_server")


def test_active_api_v1_paths_do_not_reference_legacy_relay_routes():
    root = Path(__file__).resolve().parents[2]
    targets = [
        root / 'api' / 'v1' / 'compute_provider.py',
        root / 'static' / 'chat.js',
        root / 'desktop' / 'desktop_compute_node_bridge.py',
    ]
    violations = []
    for target in targets:
        if not target.exists():
            continue
        text = target.read_text(encoding='utf-8')
        for endpoint in LEGACY:
            if endpoint in text:
                violations.append(f"{target.relative_to(root)} uses {endpoint}")

    assert not violations, 'Legacy relay routes found in active API v1 paths: ' + '; '.join(violations)
