import importlib.util
import json
import sys
from pathlib import Path


PYTHON_MODULE_DIR = Path(__file__).resolve().parents[2] / 'desktop-tauri' / 'src-tauri' / 'python'
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(PYTHON_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_MODULE_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
MODULE_PATH = PYTHON_MODULE_DIR / 'desktop_runtime_setup.py'
SPEC = importlib.util.spec_from_file_location('desktop_runtime_setup', MODULE_PATH)
desktop_runtime_setup = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules['desktop_runtime_setup'] = desktop_runtime_setup
SPEC.loader.exec_module(desktop_runtime_setup)


class _SysStub:
    platform = 'win32'
    executable = sys.executable
    prefix = sys.prefix
    argv = [str(MODULE_PATH)]


def _probe(*, backend='cpu', gpu=False, device='cpu', error=None):
    return desktop_runtime_setup.RuntimeProbe(
        backend=backend,
        gpu_offload_supported=gpu,
        detected_device=device,
        interpreter=sys.executable,
        prefix=sys.prefix,
        llama_module_path='C:/Python/Lib/site-packages/llama_cpp/__init__.py',
        error=error,
    )


def test_skip_runtime_bootstrap_for_cpu_mode(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    result = desktop_runtime_setup.ensure_desktop_llama_runtime('cpu')
    assert result['runtime_action'] == 'skipped'
    assert result['selected_backend'] == 'cpu'


def test_windows_runtime_bootstrap_auto_repairs_and_requests_reexec(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    probes = iter([_probe(), _probe(backend='cuda', gpu=True, device='cuda')])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: next(probes))
    monkeypatch.setattr(
        desktop_runtime_setup, '_windows_cuda_source_repair', lambda _requirements_path: (True, 'ok')
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')

    assert result['runtime_action'] == 'installed_cuda_reexec'
    assert result['selected_backend'] == 'cuda'


def test_runtime_bootstrap_noop_when_gpu_runtime_is_already_present(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda: _probe(backend='cuda', gpu=True, device='nvidia'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('gpu')

    assert result['runtime_action'] == 'already_supported'
    assert result['selected_backend'] == 'cuda'


def test_runtime_bootstrap_falls_back_to_cpu_when_repair_fails(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setattr(
        desktop_runtime_setup, '_windows_cuda_source_repair', lambda _requirements_path: (False, 'compile failed')
    )
    plans = [
        desktop_runtime_setup.LlamaCppInstallPlan(
            platform='win32',
            backend='cpu',
            package_spec='llama-cpp-python',
            cmake_args=None,
            force_cmake=False,
            index_url='https://pypi.org/simple',
            extra_index_url=None,
            only_binary=True,
            no_binary=False,
        )
    ]
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: plans)
    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'installed_cpu_fallback'
    assert result['selected_backend'] == 'cpu'


def test_maybe_reexec_for_runtime_refresh_reexecs_once(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    called = {}

    def fake_execve(prog, argv, env):
        called['prog'] = prog
        called['argv'] = argv
        called['guard'] = env.get(desktop_runtime_setup.REEXEC_GUARD_ENV)

    monkeypatch.setattr(desktop_runtime_setup.os, 'execve', fake_execve)
    monkeypatch.delenv(desktop_runtime_setup.REEXEC_GUARD_ENV, raising=False)

    desktop_runtime_setup.maybe_reexec_for_runtime_refresh({'runtime_action': 'installed_cuda_reexec'})

    assert called['prog'] == sys.executable
    assert called['guard'] == '1'


def test_windows_runtime_bootstrap_respects_opt_out_env(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setenv(desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV, '1')
    invoked = {'source_repair': False}
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_windows_cuda_source_repair',
        lambda _requirements_path: (invoked.update(source_repair=True), '') and (False, 'unexpected call'),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')

    assert result['runtime_action'] == 'probe_only'
    assert desktop_runtime_setup.DISABLE_BOOTSTRAP_ENV in result['fallback_reason']
    assert invoked['source_repair'] is False


def test_windows_runtime_bootstrap_success_reexec_is_guarded_to_one_attempt(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(desktop_runtime_setup, '_clear_source_repair_failure', lambda: None)
    probes = iter([_probe(), _probe(backend='cuda', gpu=True, device='cuda')])
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: next(probes))
    monkeypatch.setattr(
        desktop_runtime_setup, '_windows_cuda_source_repair', lambda _requirements_path: (True, 'ok')
    )
    exec_calls = {'count': 0}
    monkeypatch.setattr(
        desktop_runtime_setup.os,
        'execve',
        lambda *_args: exec_calls.update(count=exec_calls['count'] + 1),
    )
    monkeypatch.delenv(desktop_runtime_setup.REEXEC_GUARD_ENV, raising=False)

    runtime_setup = desktop_runtime_setup.ensure_desktop_llama_runtime('auto')
    desktop_runtime_setup.maybe_reexec_for_runtime_refresh(runtime_setup)
    monkeypatch.setenv(desktop_runtime_setup.REEXEC_GUARD_ENV, '1')
    desktop_runtime_setup.maybe_reexec_for_runtime_refresh(runtime_setup)

    assert runtime_setup['runtime_action'] == 'installed_cuda_reexec'
    assert exec_calls['count'] == 1


def test_fallback_unpinned_plans_cover_win_darwin_and_other_platforms():
    win_plans = desktop_runtime_setup._fallback_unpinned_plans('win32')
    darwin_plans = desktop_runtime_setup._fallback_unpinned_plans('darwin')
    linux_plans = desktop_runtime_setup._fallback_unpinned_plans('linux')

    assert [plan.backend for plan in win_plans] == ['cuda', 'cpu']
    assert [plan.backend for plan in darwin_plans] == ['metal', 'metal']
    assert [plan.backend for plan in linux_plans] == ['cpu']


def test_windows_source_repair_uses_active_interpreter(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    captured = {}

    def fake_run(cmd, env, timeout_seconds):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', fake_run)
    ok, _ = desktop_runtime_setup._windows_cuda_source_repair(Path.cwd() / 'requirements.txt')

    assert ok is True
    assert captured['cmd'][:3] == [sys.executable, '-m', 'pip']
    assert captured['cmd'][4].startswith('llama-cpp-python==')
    assert captured['env']['CMAKE_ARGS'] == '-DGGML_CUDA=on'
    assert captured['env']['FORCE_CMAKE'] == '1'
    assert captured['timeout_seconds'] == desktop_runtime_setup.PIP_SOURCE_BUILD_TIMEOUT_SECONDS


def test_probe_marks_error_when_subprocess_has_empty_stdout(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    class _Result:
        returncode = 1
        stdout = ''
        stderr = 'probe failed'

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _Result())

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'probe failed'


def test_maybe_reexec_for_runtime_refresh_skips_when_guard_set(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    called = {'execve': False}
    monkeypatch.setattr(desktop_runtime_setup.os, 'execve', lambda *_args: called.update(execve=True))
    monkeypatch.setenv(desktop_runtime_setup.REEXEC_GUARD_ENV, '1')

    desktop_runtime_setup.maybe_reexec_for_runtime_refresh({'runtime_action': 'installed_cuda_reexec'})

    assert called['execve'] is False


def test_maybe_reexec_for_runtime_refresh_handles_execve_oserror(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    def _raise(*_args):
        raise OSError('denied')

    monkeypatch.setattr(desktop_runtime_setup.os, 'execve', _raise)
    monkeypatch.delenv(desktop_runtime_setup.REEXEC_GUARD_ENV, raising=False)

    desktop_runtime_setup.maybe_reexec_for_runtime_refresh({'runtime_action': 'installed_cuda_reexec'})


def test_source_repair_cooldown_skips_immediate_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    state_path = tmp_path / 'runtime_state.json'
    monkeypatch.setattr(desktop_runtime_setup, '_runtime_state_path', lambda: state_path)
    now = 1_000.0
    monkeypatch.setattr(desktop_runtime_setup.time, 'time', lambda: now)
    state_path.write_text(
        json.dumps(
            {
                'source_repair_failures': {
                    sys.executable: {'last_failed_at': now - 30, 'reason': 'build failed'}
                }
            }
        ),
        encoding='utf-8',
    )
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [])

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'failed'
    assert result['fallback_reason'] == 'build failed'
