"""Unit tests for desktop Python path bootstrap behavior."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'desktop-tauri'
    / 'src-tauri'
    / 'python'
    / 'path_bootstrap.py'
)


@pytest.fixture(scope='session')
def path_bootstrap():
    spec = importlib.util.spec_from_file_location('desktop_path_bootstrap', MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_adds_resources_import_root_for_exe_python_layout(tmp_path, path_bootstrap):
    script = tmp_path / 'bin' / 'python' / 'model_bridge.py'
    resources_root = tmp_path / 'bin' / 'resources'
    (resources_root / 'utils').mkdir(parents=True)
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')

    original_sys_path = list(sys.path)
    try:
        path_bootstrap.ensure_runtime_import_paths(str(script))
        assert str(resources_root) in sys.path
        assert sys.path.index(str(resources_root)) == 0
    finally:
        sys.path[:] = original_sys_path


def test_bootstrap_adds_repo_root_for_dev_layout(tmp_path, path_bootstrap):
    script = tmp_path / 'repo' / 'desktop-tauri' / 'src-tauri' / 'python' / 'model_bridge.py'
    repo_root = tmp_path / 'repo'
    (repo_root / 'utils').mkdir(parents=True)
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')

    original_sys_path = list(sys.path)
    try:
        path_bootstrap.ensure_runtime_import_paths(str(script))
        assert str(repo_root) in sys.path
    finally:
        sys.path[:] = original_sys_path


def test_bootstrap_prefers_explicit_import_root_env(tmp_path, path_bootstrap):
    script = tmp_path / 'bin' / 'python' / 'model_bridge.py'
    explicit_root = tmp_path / 'bundle' / 'runtime-root'
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')
    (explicit_root / 'utils').mkdir(parents=True)

    original_sys_path = list(sys.path)
    previous_import_root = os.environ.get('TOKEN_PLACE_PYTHON_IMPORT_ROOT')
    try:
        os.environ['TOKEN_PLACE_PYTHON_IMPORT_ROOT'] = str(explicit_root)
        path_bootstrap.ensure_runtime_import_paths(str(script))
        assert sys.path[0] == str(explicit_root)
    finally:
        if previous_import_root is None:
            os.environ.pop('TOKEN_PLACE_PYTHON_IMPORT_ROOT', None)
        else:
            os.environ['TOKEN_PLACE_PYTHON_IMPORT_ROOT'] = previous_import_root
        sys.path[:] = original_sys_path


def test_bootstrap_resolves_nested_up_resource_layout(tmp_path, path_bootstrap):
    script = tmp_path / 'installed' / 'resources' / 'python' / 'model_bridge.py'
    nested_up_root = tmp_path / 'installed' / 'resources' / '_up_' / '_up_'
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')
    (nested_up_root / 'utils').mkdir(parents=True)

    original_sys_path = list(sys.path)
    previous_resource_dir = os.environ.get('TOKEN_PLACE_RESOURCE_DIR')
    try:
        os.environ['TOKEN_PLACE_RESOURCE_DIR'] = str(tmp_path / 'installed' / 'resources')
        path_bootstrap.ensure_runtime_import_paths(str(script))
        assert sys.path[0] == str(nested_up_root)
    finally:
        if previous_resource_dir is None:
            os.environ.pop('TOKEN_PLACE_RESOURCE_DIR', None)
        else:
            os.environ['TOKEN_PLACE_RESOURCE_DIR'] = previous_resource_dir
        sys.path[:] = original_sys_path
