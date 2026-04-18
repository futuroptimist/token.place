"""Regression coverage for lightweight runtime capability probing imports."""

import builtins
import importlib.util
from pathlib import Path


def test_model_manager_import_does_not_require_requests_dependency(monkeypatch):
    module_path = Path(__file__).resolve().parents[2] / 'utils' / 'llm' / 'model_manager.py'
    spec = importlib.util.spec_from_file_location('model_manager_without_requests', module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader

    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'requests':
            raise ModuleNotFoundError("No module named 'requests'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    spec.loader.exec_module(module)

    assert hasattr(module, 'detect_llama_runtime_capabilities')
    assert module.requests is None
