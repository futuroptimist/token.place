import json
from pathlib import Path


def test_desktop_package_all_script_includes_all_platforms():
    package_json_path = Path(__file__).resolve().parents[2] / "desktop" / "package.json"
    package_json = json.loads(package_json_path.read_text())

    scripts = package_json.get("scripts", {})
    assert "package:all" in scripts, "Expected package:all script to automate all platform builds"

    command = scripts["package:all"]
    assert "--mac" in command and "--win" in command and "--linux" in command
