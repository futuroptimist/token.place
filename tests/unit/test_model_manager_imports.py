from __future__ import annotations

import importlib
import sys


def test_model_manager_imports_without_requests_dependency(monkeypatch):
    class _BlockRequestsFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'requests' or fullname.startswith('requests.'):
                raise ModuleNotFoundError("No module named 'requests'", name='requests')
            return None

    sys.modules.pop('utils.llm.model_manager', None)
    finder = _BlockRequestsFinder()
    sys.meta_path.insert(0, finder)
    try:
        module = importlib.import_module('utils.llm.model_manager')
    finally:
        sys.meta_path.remove(finder)

    assert hasattr(module, 'ModelManager')
