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


def test_import_runtime_discovery_hang_times_out(monkeypatch):
    def _hang(_name):
        import time
        time.sleep(1.0)

    monkeypatch.setattr(model_manager.importlib.util, "find_spec", _hang)

    with pytest.raises(model_manager.LlamaCppRuntimeTimeout) as excinfo:
        model_manager._import_llama_cpp_runtime(
            require_real_runtime=True,
            timeout_seconds=0.01,
        )

    assert excinfo.value.stage == "llama_cpp_runtime_discovery"
    assert "llama_cpp_runtime_discovery_timeout" in str(excinfo.value)


def test_import_hang_times_out_after_successful_discovery(monkeypatch):
    fake_spec = types.SimpleNamespace(
        origin=(
            "C:/Users/danie/AppData/Local/Programs/Python/Python311/Lib/"
            "site-packages/llama_cpp/__init__.py"
        )
    )
    monkeypatch.setattr(model_manager.importlib.util, "find_spec", lambda _name: fake_spec)

    def _hang(_name):
        import time
        time.sleep(1.0)

    monkeypatch.setattr(model_manager.importlib, "import_module", _hang)

    with pytest.raises(model_manager.LlamaCppRuntimeTimeout) as excinfo:
        model_manager._import_llama_cpp_runtime(
            require_real_runtime=True,
            timeout_seconds=0.01,
        )

    assert excinfo.value.stage == "llama_cpp_import"
    assert "llama_cpp_import_timeout" in str(excinfo.value)


def test_repo_shim_detection_normalizes_windows_extended_paths(monkeypatch):
    monkeypatch.setattr(
        model_manager,
        "REPO_LLAMA_CPP_SHIM",
        model_manager.Path(r"C:\Users\danie\token.place desktop\llama_cpp.py"),
    )

    assert model_manager._is_repo_llama_cpp_shim(
        r"\\?\C:\Users\danie\token.place desktop\llama_cpp.py"
    )


def test_gpu_probe_hang_reports_stage_timeout():
    def _hang():
        import time
        time.sleep(1.0)

    fake_module = types.SimpleNamespace(
        __file__="C:/Python311/Lib/site-packages/llama_cpp/__init__.py",
        llama_supports_gpu_offload=_hang,
    )

    payload = model_manager.detect_llama_runtime_capabilities(
        fake_module,
        timeout_seconds=0.01,
    )

    assert payload['backend'] == 'missing'
    assert 'llama_cpp_gpu_probe_timeout' in payload['error']
