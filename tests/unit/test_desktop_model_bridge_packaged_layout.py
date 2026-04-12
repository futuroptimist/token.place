"""Regression test for desktop model bridge imports in packaged-like layouts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PYTHON_DIR = Path(__file__).resolve().parents[2] / "desktop-tauri" / "src-tauri" / "python"


def _write_model_manager_stub(package_root: Path) -> None:
    llm_dir = package_root / "utils" / "llm"
    llm_dir.mkdir(parents=True, exist_ok=True)
    (package_root / "utils" / "__init__.py").write_text("", encoding="utf-8")
    (llm_dir / "__init__.py").write_text("", encoding="utf-8")
    (llm_dir / "model_manager.py").write_text(
        "\n".join(
            [
                "class _Manager:",
                "    def get_model_artifact_metadata(self):",
                "        return {",
                "            'canonical_family_url': 'https://example.invalid/family',",
                "            'filename': 'model.gguf',",
                "            'url': 'https://example.invalid/model.gguf',",
                "            'models_dir': '/tmp/models',",
                "            'resolved_model_path': '/tmp/models/model.gguf',",
                "            'exists': False,",
                "            'size_bytes': None,",
                "        }",
                "",
                "    def download_model_if_needed(self):",
                "        return True",
                "",
                "",
                "def get_model_manager():",
                "    return _Manager()",
            ]
        ),
        encoding="utf-8",
    )


def test_model_bridge_inspect_resolves_utils_from_nested_up_dir(tmp_path):
    resources_python = tmp_path / "resources" / "python"
    resources_python.mkdir(parents=True, exist_ok=True)
    (resources_python / "model_bridge.py").write_text(
        (PYTHON_DIR / "model_bridge.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (resources_python / "path_bootstrap.py").write_text(
        (PYTHON_DIR / "path_bootstrap.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    # Simulate tauri path rewrite for ../../utils in bundle resources.
    rewritten_resource_root = tmp_path / "resources" / "_up_" / "_up_"
    _write_model_manager_stub(rewritten_resource_root)

    result = subprocess.run(
        [sys.executable, str(resources_python / "model_bridge.py"), "inspect"],
        capture_output=True,
        text=True,
        cwd=tmp_path / "resources",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["payload"]["filename"] == "model.gguf"
