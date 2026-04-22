import types

import pytest

from utils.llm import model_manager


def test_import_guard_rejects_repo_local_shim_before_import(monkeypatch):
    fake_spec = types.SimpleNamespace(origin=str(model_manager.REPO_LLAMA_CPP_SHIM))
    monkeypatch.setattr(model_manager.importlib.util, "find_spec", lambda _name: fake_spec)
    monkeypatch.setitem(model_manager.sys.modules, "llama_cpp", object())

    import_attempted = False

    def _unexpected_import(_name):
        nonlocal import_attempted
        import_attempted = True
        return object()

    monkeypatch.setattr(model_manager.importlib, "import_module", _unexpected_import)

    with pytest.raises(ImportError, match="repository-local llama_cpp.py shim"):
        model_manager._import_llama_cpp_runtime(require_real_runtime=True)

    assert not import_attempted
    assert "llama_cpp" not in model_manager.sys.modules


def test_import_guard_rejects_repo_local_llama_cpp_shim(monkeypatch):
    fake_module = types.SimpleNamespace(
        __file__=str(model_manager.REPO_LLAMA_CPP_SHIM),
        Llama=object,
    )

    monkeypatch.setattr(model_manager.importlib, "import_module", lambda _name: fake_module)

    with pytest.raises(ImportError, match="repository-local llama_cpp.py shim"):
        model_manager._import_llama_cpp_runtime(require_real_runtime=True)


def test_import_guard_allows_repo_shim_when_real_runtime_not_required(monkeypatch):
    fake_spec = types.SimpleNamespace(origin=str(model_manager.REPO_LLAMA_CPP_SHIM))
    fake_module = types.SimpleNamespace(__file__=str(model_manager.REPO_LLAMA_CPP_SHIM), Llama=object)
    monkeypatch.setattr(model_manager.importlib.util, "find_spec", lambda _name: fake_spec)
    monkeypatch.setattr(model_manager.importlib, "import_module", lambda _name: fake_module)

    loaded = model_manager._import_llama_cpp_runtime(require_real_runtime=False)
    assert loaded is fake_module
