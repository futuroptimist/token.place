import json
from pathlib import Path

import pytest

PACKAGE_JSON = Path(__file__).resolve().parents[2] / "package.json"


@pytest.mark.unit
def test_npm_lint_script_invokes_eslint():
    package_data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    scripts = package_data.get("scripts", {})
    lint_script = scripts.get("lint")
    assert lint_script, "Expected `lint` script to exist in package.json"
    assert "eslint" in lint_script, (
        "`npm run lint` should invoke eslint so documented lint instructions catch regressions"
    )
