"""Unit tests for desktop Python bridge import path bootstrapping."""

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'desktop-tauri'
    / 'src-tauri'
    / 'python'
    / 'path_bootstrap.py'
)
SPEC = importlib.util.spec_from_file_location('desktop_path_bootstrap', MODULE_PATH)
path_bootstrap = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(path_bootstrap)


def test_bootstrap_supports_exe_python_with_sibling_resources_up(tmp_path):
    script_file = tmp_path / 'build' / 'debug' / 'python' / 'model_bridge.py'
    script_file.parent.mkdir(parents=True)
    script_file.write_text('# bridge')

    import_root = tmp_path / 'build' / 'debug' / 'resources' / '_up_'
    (import_root / 'utils').mkdir(parents=True)

    original_sys_path = sys.path.copy()
    try:
        path_bootstrap.ensure_runtime_import_paths(str(script_file))
        assert str(import_root) in sys.path
        assert sys.path.index(str(import_root)) == 0
    finally:
        sys.path[:] = original_sys_path
