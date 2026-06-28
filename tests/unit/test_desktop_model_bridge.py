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

    with patch.object(model_bridge, '_run_dependency_preflight', return_value={'ok': True}):
        with patch.object(model_bridge, '_get_model_manager', return_value=(manager, None)):
            status = model_bridge.inspect_model()

    assert status == 0
    manager.get_model_artifact_metadata.assert_called_once_with()
    assert json.loads(capsys.readouterr().out.strip()) == {'ok': True, 'payload': metadata}


def test_inspect_returns_bridge_error_when_manager_init_fails(capsys):
    with patch.object(model_bridge, '_run_dependency_preflight', return_value={'ok': True}):
        with patch.object(model_bridge, '_get_model_manager', return_value=(None, {'ok': False, 'error': 'boom'})):
            status = model_bridge.inspect_model()

    assert status == 1
    assert json.loads(capsys.readouterr().out.strip()) == {'ok': False, 'error': 'boom'}


def test_download_returns_actionable_error_when_download_fails(capsys):
    manager = MagicMock()
    manager.download_model_if_needed.return_value = False

    with patch.object(model_bridge, '_run_dependency_preflight', return_value={'ok': True}):
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

    with patch.object(model_bridge, '_run_dependency_preflight', return_value={'ok': True}):
        with patch.object(model_bridge, '_get_model_manager', return_value=(manager, None)):
            status = model_bridge.download_model()

    assert status == 0
    manager.download_model_if_needed.assert_called_once_with()
    manager.get_model_artifact_metadata.assert_called_once_with()
    assert json.loads(capsys.readouterr().out.strip()) == {'ok': True, 'payload': metadata}


def test_inspect_fails_when_dependency_preflight_fails(capsys):
    with patch.object(model_bridge, '_run_dependency_preflight', return_value={'ok': False, 'error': 'deps bad'}):
        status = model_bridge.inspect_model()

    assert status == 1
    assert json.loads(capsys.readouterr().out.strip()) == {'ok': False, 'error': 'deps bad'}


def test_get_model_manager_reports_missing_dependency(capsys):
    with patch.dict('sys.modules', {'utils.llm.model_manager': None}):
        manager, error_status = model_bridge._get_model_manager()

    assert manager is None
    assert error_status == {
        'ok': False,
        'error': "Missing Python dependency for model downloads (import of utils.llm.model_manager halted; None in sys.modules).",
    }
    assert capsys.readouterr().out.strip() == ''




def test_fallback_model_metadata_uses_platform_specific_models_dir(monkeypatch):
    monkeypatch.delenv('TOKEN_PLACE_MODELS_DIR', raising=False)
    monkeypatch.setattr(model_bridge, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(model_bridge, 'os', SimpleNamespace(name='posix', environ={}))
    payload = model_bridge._fallback_model_metadata()
    assert '/Library/Application Support/' not in payload['models_dir']


def test_default_models_dir_windows_uses_appdata_when_present(monkeypatch):
    monkeypatch.setattr(model_bridge, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(model_bridge, 'os', SimpleNamespace(name='nt', environ={'APPDATA': r'C:\\Users\\runner\\AppData\\Roaming'}))

    models_dir = model_bridge._default_models_dir()

    assert str(models_dir) == r'C:\\Users\\runner\\AppData\\Roaming/token.place/models'


def test_default_models_dir_uses_xdg_data_home_on_posix(monkeypatch):
    monkeypatch.setattr(model_bridge, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(model_bridge, 'os', SimpleNamespace(name='posix', environ={'XDG_DATA_HOME': '/tmp/xdg-data'}))

    models_dir = model_bridge._default_models_dir()

    assert str(models_dir) == '/tmp/xdg-data/token.place/models'

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


def test_is_inspect_optional_missing_matches_exception_name():
    assert model_bridge._is_inspect_optional_missing(
        ModuleNotFoundError("No module named 'psutil'", name='psutil')
    )
    assert model_bridge._is_inspect_optional_missing(
        ModuleNotFoundError("No module named 'requests'", name='requests')
    )


def test_is_inspect_optional_missing_matches_message_when_name_absent():
    assert model_bridge._is_inspect_optional_missing(
        ModuleNotFoundError("No module named 'urllib3'")
    )
    assert model_bridge._is_inspect_optional_missing(
        ModuleNotFoundError("No module named 'dotenv'")
    )


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


def test_inspect_returns_fallback_when_requests_missing(capsys):
    real_import = __import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'utils.llm.model_manager':
            raise ModuleNotFoundError("No module named 'requests'", name='requests')
        return real_import(name, globals, locals, fromlist, level)

    with patch('builtins.__import__', side_effect=_fake_import):
        status = model_bridge.inspect_model()

    assert status == 0
    response = json.loads(capsys.readouterr().out.strip())
    assert response['ok'] is True
    for key in ('canonical_family_url','filename','url','models_dir','resolved_model_path','exists','size_bytes'):
        assert key in response['payload']

def test_inspect_returns_fallback_when_dotenv_missing(capsys):
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

    assert status == 0
    response = json.loads(capsys.readouterr().out.strip())
    assert response['ok'] is True
    for key in ('canonical_family_url','filename','url','models_dir','resolved_model_path','exists','size_bytes'):
        assert key in response['payload']


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
    model_profiles_path = Path('utils/llm/model_profiles.py')
    python_dir.mkdir(parents=True)
    utils_llm_dir.mkdir(parents=True)

    (python_dir / 'model_bridge.py').write_text(MODULE_PATH.read_text(encoding='utf-8'), encoding='utf-8')
    path_bootstrap_path = MODULE_PATH.parent / 'path_bootstrap.py'
    (python_dir / 'path_bootstrap.py').write_text(path_bootstrap_path.read_text(encoding='utf-8'), encoding='utf-8')
    desktop_runtime_setup_path = MODULE_PATH.parent / 'desktop_runtime_setup.py'
    (python_dir / 'desktop_runtime_setup.py').write_text(desktop_runtime_setup_path.read_text(encoding='utf-8'), encoding='utf-8')
    desktop_gpu_packaging_path = MODULE_PATH.parent / 'desktop_gpu_packaging.py'
    (python_dir / 'desktop_gpu_packaging.py').write_text(desktop_gpu_packaging_path.read_text(encoding='utf-8'), encoding='utf-8')
    (python_dir / 'requirements_desktop_runtime.txt').write_text('psutil==7.1.0\nrequests==2.32.5\npython-dotenv==1.1.1\ncryptography==46.0.1\n', encoding='utf-8')
    (import_root / 'utils' / '__init__.py').write_text('', encoding='utf-8')
    (utils_llm_dir / '__init__.py').write_text('', encoding='utf-8')
    (utils_llm_dir / 'model_profiles.py').write_text(model_profiles_path.read_text(encoding='utf-8'), encoding='utf-8')
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


def test_run_dependency_preflight_formats_failure_context():
    with patch.object(
        model_bridge,
        'ensure_desktop_python_dependencies',
        return_value={'ok': 'false', 'missing': 'cryptography', 'action': 'install_failed', 'detail': 'permission denied'},
    ):
        payload = model_bridge._run_dependency_preflight()

    assert payload['ok'] is False
    assert 'missing=cryptography' in payload['error']
    assert 'action=install_failed' in payload['error']
    assert 'detail=permission denied' in payload['error']


def test_main_returns_json_error_on_unhandled_exception(capsys):
    with patch.object(model_bridge.argparse.ArgumentParser, 'parse_args', return_value=SimpleNamespace(action='inspect')):
        with patch.object(model_bridge, 'inspect_model', side_effect=RuntimeError('unexpected boom')):
            status = model_bridge.main()

    assert status == 1
    assert json.loads(capsys.readouterr().out.strip()) == {
        'ok': False,
        'error': 'Model bridge failure: unexpected boom',
    }


def test_download_fails_when_dependency_preflight_fails(capsys):
    with patch.object(model_bridge, '_run_dependency_preflight', return_value={'ok': False, 'error': 'deps bad'}):
        status = model_bridge.download_model()

    assert status == 1
    assert json.loads(capsys.readouterr().out.strip()) == {'ok': False, 'error': 'deps bad'}


def test_fallback_model_metadata_reports_llama_profile_by_default(monkeypatch):
    monkeypatch.delenv('TOKEN_PLACE_DEFAULT_MODEL_FILENAME', raising=False)
    monkeypatch.delenv('TOKEN_PLACE_DEFAULT_MODEL_URL', raising=False)
    monkeypatch.delenv('TOKEN_PLACE_DEFAULT_MODEL_FAMILY_URL', raising=False)
    monkeypatch.setenv('TOKEN_PLACE_MODELS_DIR', '/tmp/token-place-models')

    payload = model_bridge._fallback_model_metadata()

    assert payload['api_model_id'] == 'llama-3.1-8b-instruct'
    assert payload['profile_id'] == 'llama-3.1-8b-q4-k-m'
    assert payload['display_name'] == 'Meta Llama 3.1 8B Instruct'
    assert payload['filename'] == 'Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf'
    assert payload['url'].endswith('/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf')
    assert payload['canonical_family_url'] == 'https://huggingface.co/meta-llama/Meta-Llama-3-8B'


def test_fallback_model_metadata_preserves_model_env_overrides(monkeypatch):
    monkeypatch.setenv('TOKEN_PLACE_DEFAULT_MODEL_FILENAME', 'override.gguf')
    monkeypatch.setenv('TOKEN_PLACE_DEFAULT_MODEL_URL', 'https://example.com/override.gguf')
    monkeypatch.setenv('TOKEN_PLACE_DEFAULT_MODEL_FAMILY_URL', 'https://example.com/family')
    monkeypatch.setenv('TOKEN_PLACE_MODELS_DIR', '/tmp/token-place-models')

    payload = model_bridge._fallback_model_metadata()

    assert payload['profile_id'] == 'llama-3.1-8b-q4-k-m'
    assert payload['filename'] == 'override.gguf'
    assert payload['url'] == 'https://example.com/override.gguf'
    assert payload['canonical_family_url'] == 'https://example.com/family'
