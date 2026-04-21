import types

import pytest

from utils.llm import model_manager


def test_import_guard_rejects_repo_local_llama_cpp_shim(monkeypatch):
    fake_module = types.SimpleNamespace(
        __file__=str(model_manager.REPO_LLAMA_CPP_SHIM),
        Llama=object,
    )

    monkeypatch.setattr(model_manager.importlib, "import_module", lambda _name: fake_module)

    with pytest.raises(ImportError, match="repository-local llama_cpp.py shim"):
        model_manager._import_llama_cpp_runtime(require_real_runtime=True)
