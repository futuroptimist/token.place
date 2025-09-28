import json
from pathlib import Path

import pytest


@pytest.mark.unit
def test_electron_builder_targets_cover_cross_platform_installers():
    config_path = Path(__file__).resolve().parents[2] / "desktop" / "electron-builder.json"
    config = json.loads(config_path.read_text())

    def extract_targets(target_spec):
        if isinstance(target_spec, str):
            return {target_spec}
        if isinstance(target_spec, list):
            targets = set()
            for item in target_spec:
                targets.update(extract_targets(item))
            return targets
        if isinstance(target_spec, dict):
            targets = set()
            if "target" in target_spec:
                targets.update(extract_targets(target_spec["target"]))
            return targets
        return set()

    mac_targets = extract_targets(config.get("mac", {}).get("target", []))
    win_targets = extract_targets(config.get("win", {}).get("target", []))
    linux_targets = extract_targets(config.get("linux", {}).get("target", []))

    assert {"dmg", "pkg"}.issubset(mac_targets)
    assert {"nsis", "msi"}.issubset(win_targets)
    assert {"deb", "rpm"}.issubset(linux_targets)
