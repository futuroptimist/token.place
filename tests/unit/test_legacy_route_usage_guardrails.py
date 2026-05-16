from pathlib import Path
import inspect
import re

from utils.compute_node_runtime import ComputeNodeRuntime
from utils.networking.relay_client import RelayClient

LEGACY_ROUTES = ("/sink", "/faucet", "/source", "/retrieve", "/next_server")


def test_active_production_paths_do_not_reference_legacy_relay_routes():
    root = Path(__file__).resolve().parents[2]
    targets = [
        root / "api" / "v1" / "compute_provider.py",
        root / "client.py",
        root / "utils" / "crypto_helpers.py",
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



def test_api_v1_polling_loop_does_not_call_legacy_relay_routes():
    """The active compute-node polling loop must stay on API v1 E2EE routes."""
    source = inspect.getsource(RelayClient.poll_relay_continuously)

    assert "ping_relay" not in source
    for route in LEGACY_ROUTES:
        assert route not in source


def test_compute_node_runtime_default_adapters_are_api_v1_only():
    """Default runtime processing must not enable legacy sink/source adapters."""
    source = inspect.getsource(ComputeNodeRuntime.__init__)

    assert "ApiV1RelayRequestAdapter" in source
    assert "LegacyRelayRequestAdapter(self.relay_client)" not in source
