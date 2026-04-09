"""Unit tests for the desktop Python model bridge."""

import importlib.util
import json
from pathlib import Path
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
