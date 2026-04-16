"""Unit tests for desktop Python path bootstrap behavior."""

import importlib.util
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


def test_bootstrap_supports_nested_up_packaged_layout(tmp_path, path_bootstrap):
    script = tmp_path / 'bin' / 'resources' / 'python' / 'model_bridge.py'
    import_root = tmp_path / 'bin' / 'resources' / '_up_' / '_up_'
    (import_root / 'utils').mkdir(parents=True)
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')

    original_sys_path = list(sys.path)
    try:
        path_bootstrap.ensure_runtime_import_paths(str(script))
        assert str(import_root) in sys.path
        assert sys.path.index(str(import_root)) == 0
    finally:
        sys.path[:] = original_sys_path


def test_bootstrap_prefers_explicit_env_import_root(tmp_path, path_bootstrap, monkeypatch):
    script = tmp_path / 'bin' / 'resources' / 'python' / 'model_bridge.py'
    fallback_root = tmp_path / 'bin' / 'resources' / '_up_'
    explicit_root = tmp_path / 'explicit-import-root'
    (fallback_root / 'utils').mkdir(parents=True)
    (explicit_root / 'utils').mkdir(parents=True)
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')
    monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(explicit_root))

    original_sys_path = list(sys.path)
    try:
        path_bootstrap.ensure_runtime_import_paths(str(script))
        assert str(explicit_root) in sys.path
        assert sys.path.index(str(explicit_root)) == 0
    finally:
        sys.path[:] = original_sys_path


def test_bootstrap_keeps_repo_root_importable_without_shadowing_llama_cpp(
    tmp_path, path_bootstrap, monkeypatch
):
    script = tmp_path / 'repo' / 'desktop-tauri' / 'src-tauri' / 'python' / 'model_bridge.py'
    repo_root = tmp_path / 'repo'
    (repo_root / 'utils').mkdir(parents=True)
    (repo_root / 'llama_cpp.py').write_text('# shim\n', encoding='utf-8')
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')

    original_sys_path = list(sys.path)
    try:
        monkeypatch.chdir(repo_root)
        # Simulate startup from repo root so `''` would shadow llama_cpp.
        sys.path.insert(0, '')
        path_bootstrap.ensure_runtime_import_paths(str(script), avoid_llama_cpp_shadowing=True)
        assert str(repo_root) in sys.path
        repo_index = sys.path.index(str(repo_root))
        site_packages_indices = [i for i, entry in enumerate(sys.path) if 'site-packages' in entry]
        if site_packages_indices:
            assert repo_index > max(site_packages_indices)
        assert '' not in sys.path
    finally:
        sys.path[:] = original_sys_path


def test_bootstrap_readds_explicit_cwd_when_only_empty_entry_present(tmp_path, path_bootstrap, monkeypatch):
    script = tmp_path / 'repo' / 'desktop-tauri' / 'src-tauri' / 'python' / 'model_bridge.py'
    repo_root = tmp_path / 'repo'
    (repo_root / 'utils').mkdir(parents=True)
    (repo_root / 'llama_cpp.py').write_text('# shim\n', encoding='utf-8')
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')

    original_sys_path = list(sys.path)
    try:
        monkeypatch.chdir(repo_root)
        sys.path[:] = ['']
        path_bootstrap.ensure_runtime_import_paths(str(script), avoid_llama_cpp_shadowing=True)
        assert '' not in sys.path
        assert str(repo_root.resolve()) in sys.path
    finally:
        sys.path[:] = original_sys_path


def test_bootstrap_preserves_repo_utils_imports_while_preventing_llama_shadowing(
    tmp_path, path_bootstrap, monkeypatch
):
    repo_root = tmp_path / 'repo'
    script = repo_root / 'desktop-tauri' / 'src-tauri' / 'python' / 'model_bridge.py'
    site_packages = tmp_path / 'venv' / 'Lib' / 'site-packages'
    utils_pkg = repo_root / 'utils'
    llm_pkg = utils_pkg / 'llm'

    llm_pkg.mkdir(parents=True)
    script.parent.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    (utils_pkg / '__init__.py').write_text('', encoding='utf-8')
    (llm_pkg / '__init__.py').write_text('', encoding='utf-8')
    (llm_pkg / 'model_manager.py').write_text(
        'import importlib\n'
        'def initialize_model_runtime():\n'
        '    module = importlib.import_module("llama_cpp")\n'
        '    return module.__file__\n',
        encoding='utf-8',
    )
    (repo_root / 'llama_cpp.py').write_text('SOURCE = "repo-shim"\n', encoding='utf-8')
    (site_packages / 'llama_cpp.py').write_text('SOURCE = "site-packages"\n', encoding='utf-8')
    script.write_text('# bridge\n', encoding='utf-8')

    original_sys_path = list(sys.path)
    original_modules = dict(sys.modules)
    try:
        monkeypatch.chdir(repo_root)
        sys.path[:] = ['', str(site_packages)]
        path_bootstrap.ensure_runtime_import_paths(str(script), avoid_llama_cpp_shadowing=True)
        for module_name in ("utils", "utils.llm", "utils.llm.model_manager", "llama_cpp"):
            sys.modules.pop(module_name, None)

        from utils.llm import model_manager  # noqa: PLC0415

        llama_module_path = Path(model_manager.initialize_model_runtime()).resolve()
        imported_utils_path = Path(model_manager.__file__).resolve()

        assert imported_utils_path.is_relative_to(repo_root.resolve())
        assert llama_module_path == (site_packages / 'llama_cpp.py').resolve()
        assert llama_module_path != (repo_root / 'llama_cpp.py').resolve()
    finally:
        sys.path[:] = original_sys_path
        sys.modules.clear()
        sys.modules.update(original_modules)


def test_bootstrap_adds_resolved_cwd_when_candidate_uses_non_resolved_path(
    tmp_path, path_bootstrap, monkeypatch
):
    repo_root = tmp_path / 'repo'
    script = repo_root / 'desktop-tauri' / 'src-tauri' / 'python' / 'model_bridge.py'
    aliased_repo = tmp_path / 'alias' / '..' / 'repo'
    (repo_root / 'utils').mkdir(parents=True)
    (repo_root / 'llama_cpp.py').write_text('# shim\n', encoding='utf-8')
    script.parent.mkdir(parents=True)
    script.write_text('# bridge\n', encoding='utf-8')

    original_sys_path = list(sys.path)
    try:
        monkeypatch.chdir(repo_root)
        monkeypatch.setenv('TOKEN_PLACE_PYTHON_IMPORT_ROOT', str(aliased_repo))
        sys.path[:] = ['', str(tmp_path / 'venv' / 'site-packages')]

        path_bootstrap.ensure_runtime_import_paths(str(script), avoid_llama_cpp_shadowing=True)

        assert '' not in sys.path
        assert str(repo_root.resolve()) in sys.path
    finally:
        sys.path[:] = original_sys_path
