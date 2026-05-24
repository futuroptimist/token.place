"""Unit tests for the desktop Python model bridge."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'src-tauri' / 'python' / 'model_bridge.py'
SPEC = importlib.util.spec_from_file_location('desktop_model_bridge', MODULE_PATH)
model_bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(model_bridge)


def test_inspect_returns_shared_model_manager_metadata(capsys):
    metadata = {
        'canonical_family_url': 'https://example.com/family',
        'filename': 'model.gguf',
        'url': 'https://example.com/model.gguf',
        'models_dir': '/tmp/models',
        'resolved_model_path': '/tmp/models/model.gguf',
        'exists': False,
        'size_bytes': None,
    }
    manager = MagicMock()
    manager.get_model_artifact_metadata.return_value = metadata

    with patch.object(model_bridge, '_get_model_manager', return_value=(manager, None)):
        status = model_bridge.inspect_model()

    assert status == 0
    manager.get_model_artifact_metadata.assert_called_once_with()
    assert json.loads(capsys.readouterr().out.strip()) == {'ok': True, 'payload': metadata}


def test_inspect_returns_bridge_error_when_manager_init_fails(capsys):
    with patch.object(model_bridge, '_get_model_manager', return_value=(None, {'ok': False, 'error': 'boom'})):
        status = model_bridge.inspect_model()

    assert status == 1
    assert json.loads(capsys.readouterr().out.strip()) == {'ok': False, 'error': 'boom'}


def test_download_returns_actionable_error_when_download_fails(capsys):
    manager = MagicMock()
    manager.download_model_if_needed.return_value = False

    with patch.object(model_bridge, '_get_model_manager', return_value=(manager, None)):
        status = model_bridge.download_model()

    assert status == 1
    manager.download_model_if_needed.assert_called_once_with()
    assert json.loads(capsys.readouterr().out.strip()) == {
        'ok': False,
        'error': (
            'Download failed. Verify network access to Hugging Face and check that '
            'the models directory is writable.'
        ),
    }


def test_download_returns_metadata_when_download_succeeds(capsys):
    metadata = {
        'canonical_family_url': 'https://example.com/family',
        'filename': 'model.gguf',
        'url': 'https://example.com/model.gguf',
        'models_dir': '/tmp/models',
        'resolved_model_path': '/tmp/models/model.gguf',
        'exists': True,
        'size_bytes': 2048,
    }
    manager = MagicMock()
    manager.download_model_if_needed.return_value = True
    manager.get_model_artifact_metadata.return_value = metadata

    with patch.object(model_bridge, '_get_model_manager', return_value=(manager, None)):
        status = model_bridge.download_model()

    assert status == 0
    manager.download_model_if_needed.assert_called_once_with()
    manager.get_model_artifact_metadata.assert_called_once_with()
    assert json.loads(capsys.readouterr().out.strip()) == {'ok': True, 'payload': metadata}


def test_get_model_manager_reports_missing_dependency(capsys):
    with patch.dict('sys.modules', {'utils.llm.model_manager': None}):
        manager, error_status = model_bridge._get_model_manager()

    assert manager is None
    assert error_status == {
        'ok': False,
        'error': "Missing Python dependency for model downloads (import of utils.llm.model_manager halted; None in sys.modules).",
    }
    assert capsys.readouterr().out.strip() == ''


def test_get_model_manager_treats_optional_import_failure_as_nonfatal_for_inspect(capsys):
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError("No module named 'psutil'", name='psutil')
        return real_import(name, globals, locals, fromlist, level)

    with patch('builtins.__import__', side_effect=_fake_import):
        manager, error_status = model_bridge._get_model_manager(allow_inspect_fallback=True)

    assert manager is None
    assert error_status['ok'] is True
    payload = error_status['payload']
    for key in ('canonical_family_url','filename','url','models_dir','resolved_model_path','exists','size_bytes'):
        assert key in payload
    assert capsys.readouterr().out.strip() == ''


def test_download_does_not_treat_optional_dependency_as_nonfatal(capsys):
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError("No module named 'requests'", name='requests')
        return real_import(name, globals, locals, fromlist, level)

    with patch('builtins.__import__', side_effect=_fake_import):
        status = model_bridge.download_model()

    assert status == 1
    response = json.loads(capsys.readouterr().out.strip())
    assert response['ok'] is False
    assert 'Missing Python dependency for model downloads' in response['error']


def test_inspect_returns_error_when_requests_missing(capsys):
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError("No module named 'requests'", name='requests')
        return real_import(name, globals, locals, fromlist, level)

    with patch('builtins.__import__', side_effect=_fake_import):
        status = model_bridge.inspect_model()

    assert status == 1
    response = json.loads(capsys.readouterr().out.strip())
    assert response['ok'] is False
    assert "No module named 'requests'" in response['error']


def test_inspect_returns_error_when_dotenv_missing(capsys):
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError(
                "No module named 'dotenv'",
                name='dotenv',
            )
        return real_import(name, globals, locals, fromlist, level)

    with patch('builtins.__import__', side_effect=_fake_import):
        status = model_bridge.inspect_model()

    assert status == 1
    response = json.loads(capsys.readouterr().out.strip())
    assert response['ok'] is False
    assert "No module named 'dotenv'" in response['error']


def test_main_dispatches_inspect_action():
    with patch.object(model_bridge.argparse.ArgumentParser, 'parse_args', return_value=SimpleNamespace(action='inspect')):
        with patch.object(model_bridge, 'inspect_model', return_value=0) as inspect_model:
            assert model_bridge.main() == 0

    inspect_model.assert_called_once_with()


def test_main_dispatches_download_action():
    with patch.object(model_bridge.argparse.ArgumentParser, 'parse_args', return_value=SimpleNamespace(action='download')):
        with patch.object(model_bridge, 'download_model', return_value=0) as download_model:
            assert model_bridge.main() == 0

    download_model.assert_called_once_with()


def test_inspect_subprocess_succeeds_for_packaged_layout_without_pythonpath(tmp_path):
    """Regression: packaged resources with ../../utils failed with No module named 'utils'."""
    python_dir = tmp_path / 'bin' / 'resources' / 'python'
    resources_dir = tmp_path / 'bin' / 'resources'
    import_root = resources_dir / '_up_' / '_up_'
    utils_llm_dir = import_root / 'utils' / 'llm'
    python_dir.mkdir(parents=True)
    utils_llm_dir.mkdir(parents=True)

    (python_dir / 'model_bridge.py').write_text(MODULE_PATH.read_text(encoding='utf-8'), encoding='utf-8')
    path_bootstrap_path = MODULE_PATH.parent / 'path_bootstrap.py'
    (python_dir / 'path_bootstrap.py').write_text(path_bootstrap_path.read_text(encoding='utf-8'), encoding='utf-8')
    (import_root / 'utils' / '__init__.py').write_text('', encoding='utf-8')
    (utils_llm_dir / '__init__.py').write_text('', encoding='utf-8')
    (utils_llm_dir / 'model_manager.py').write_text(
        """
class _Manager:
    def get_model_artifact_metadata(self):
        return {"filename": "mock.gguf", "exists": False}


def get_model_manager():
    return _Manager()
""".strip()
        + "\n",
        encoding='utf-8',
    )

    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    result = subprocess.run(
        [sys.executable, str(python_dir / 'model_bridge.py'), 'inspect'],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload['ok'] is True
    assert payload['payload']['filename'] == 'mock.gguf'
    assert "Missing Python dependency for model downloads" not in result.stdout


def test_download_returns_error_when_requests_missing(capsys):
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError("No module named 'requests'", name='requests')
        return real_import(name, globals, locals, fromlist, level)

    with patch('builtins.__import__', side_effect=_fake_import):
        status = model_bridge.download_model()

    assert status == 1
    response = json.loads(capsys.readouterr().out.strip())
    assert response['ok'] is False
    assert "No module named 'requests'" in response['error']


def test_download_returns_error_when_dotenv_missing(capsys):
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError("No module named 'dotenv'", name='dotenv')
        return real_import(name, globals, locals, fromlist, level)

    with patch('builtins.__import__', side_effect=_fake_import):
        status = model_bridge.download_model()

    assert status == 1
    response = json.loads(capsys.readouterr().out.strip())
    assert response['ok'] is False
    assert "No module named 'dotenv'" in response['error']
