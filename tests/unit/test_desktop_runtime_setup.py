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


def test_windows_runtime_bootstrap_surfaces_source_repair_detail_when_probe_stays_cpu(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    captured = {}

    def fake_record(reason):
        captured['reason'] = reason

    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', fake_record)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_windows_cuda_source_repair',
        lambda _requirements_path: (True, 'line one\nfinal pip status (metadata warning)'),
    )
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [])

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=Path.cwd())

    assert result['runtime_action'] == 'failed'
    assert 'source repair detail: final pip status (metadata warning)' in result['fallback_reason']
    assert 'source repair detail: final pip status (metadata warning)' in captured['reason']


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

    assert len(win_plans) == len(desktop_runtime_setup.CUDA_WHEEL_INDEXES) + 1
    assert [plan.backend for plan in win_plans[:-1]] == ['cuda'] * len(
        desktop_runtime_setup.CUDA_WHEEL_INDEXES
    )
    assert [plan.index_url for plan in win_plans[:-1]] == list(desktop_runtime_setup.CUDA_WHEEL_INDEXES)
    assert all(plan.extra_index_url is None for plan in win_plans[:-1])
    assert win_plans[-1].backend == 'cpu'
    assert [plan.backend for plan in darwin_plans] == ['metal', 'metal']
    assert [plan.backend for plan in linux_plans] == ['cpu']


def test_windows_source_repair_uses_active_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    requirements_path = tmp_path / 'requirements.txt'
    requirements_path.write_text('llama_cpp_python==0.3.16\n', encoding='utf-8')
    captured = {}

    def fake_run(cmd, env, timeout_seconds):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', fake_run)
    ok, _ = desktop_runtime_setup._windows_cuda_source_repair(requirements_path)

    assert ok is True
    assert captured['cmd'][:3] == [sys.executable, '-m', 'pip']
    assert captured['cmd'][3] == 'install'
    assert captured['cmd'][4:9] == [
        '--force-reinstall',
        '--no-cache-dir',
        '--no-binary',
        'llama-cpp-python',
        '--verbose',
    ]
    assert captured['cmd'][9].startswith('llama-cpp-python==')
    assert captured['env']['CMAKE_ARGS'] == '-DGGML_CUDA=on'
    assert captured['env']['FORCE_CMAKE'] == '1'
    assert captured['timeout_seconds'] == desktop_runtime_setup.PIP_SOURCE_BUILD_TIMEOUT_SECONDS


def test_windows_source_repair_returns_actionable_message_when_requirements_missing(monkeypatch, tmp_path):
    missing_requirements = tmp_path / 'AppData' / 'requirements.txt'
    captured = {}

    def fake_run(cmd, env, timeout_seconds):
        captured['cmd'] = cmd
        captured['env'] = env
        captured['timeout_seconds'] = timeout_seconds
        return True, 'ok'

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', fake_run)
    ok, reason = desktop_runtime_setup._windows_cuda_source_repair(missing_requirements)

    assert ok is True
    assert 'requirements file not found' in reason
    assert 'falling back to unpinned llama-cpp-python source reinstall' in reason
    assert str(missing_requirements) in reason
    assert captured['cmd'][9] == 'llama-cpp-python'


def test_windows_source_repair_returns_actionable_message_when_requirement_is_unreadable(monkeypatch, tmp_path):
    unreadable_requirements = tmp_path / 'AppData' / 'requirements.txt'

    def _raise_unreadable(_requirements_path):
        raise OSError('permission denied')

    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_requirement_spec', _raise_unreadable)

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))
    ok, reason = desktop_runtime_setup._windows_cuda_source_repair(unreadable_requirements)

    assert ok is True
    assert 'unable to resolve pinned llama-cpp-python requirement' in reason
    assert 'falling back to unpinned source reinstall' in reason
    assert str(unreadable_requirements) in reason
    assert 'permission denied' in reason


def test_windows_source_repair_preserves_metadata_warning_in_last_line(monkeypatch, tmp_path):
    missing_requirements = tmp_path / 'missing-requirements.txt'
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (False, 'line one\nfinal pip error line'),
    )

    ok, reason = desktop_runtime_setup._windows_cuda_source_repair(missing_requirements)

    assert ok is False
    assert 'requirements file not found at' in reason.splitlines()[-1]
    assert 'falling back to unpinned llama-cpp-python source reinstall' in reason.splitlines()[-1]


def test_windows_source_repair_returns_actionable_message_when_requirement_is_invalid(monkeypatch, tmp_path):
    invalid_requirements = tmp_path / 'AppData' / 'requirements.txt'

    def _raise_invalid(_requirements_path):
        raise ValueError('missing pinned llama-cpp-python requirement')

    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_requirement_spec', _raise_invalid)

    monkeypatch.setattr(desktop_runtime_setup, '_run_pip_install', lambda *_args, **_kwargs: (True, 'ok'))
    ok, reason = desktop_runtime_setup._windows_cuda_source_repair(invalid_requirements)

    assert ok is True
    assert 'unable to resolve pinned llama-cpp-python requirement' in reason
    assert 'falling back to unpinned source reinstall' in reason
    assert str(invalid_requirements) in reason
    assert 'missing pinned llama-cpp-python requirement' in reason


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


def test_probe_marks_error_when_subprocess_raises(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    def _raise(*_args, **_kwargs):
        raise RuntimeError('subprocess unavailable')

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', _raise)

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'subprocess unavailable'


def test_probe_uses_return_code_when_stderr_is_empty(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    class _Result:
        returncode = 9
        stdout = ''
        stderr = ''

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _Result())

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'probe subprocess failed with return code 9'


def test_probe_subprocess_sanitizes_repo_root_before_llama_import(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    captured = {}

    class _Result:
        returncode = 0
        stdout = json.dumps(
            {
                'backend': 'cuda',
                'gpu_offload_supported': True,
                'detected_device': 'cuda',
                'interpreter': sys.executable,
                'prefix': sys.prefix,
                'llama_module_path': 'C:/Python/Lib/site-packages/llama_cpp/__init__.py',
            }
        )
        stderr = ''

    def _fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['env'] = kwargs.get('env', {})
        return _Result()

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', _fake_run)
    probe = desktop_runtime_setup._probe_llama_runtime()

    assert probe.backend == 'cuda'
    assert 'ensure_runtime_import_paths' in desktop_runtime_setup._PROBE_SNIPPET
    assert "Path(entry or \".\").resolve()" in desktop_runtime_setup._PROBE_SNIPPET
    assert captured['cmd'][:2] == [sys.executable, '-c']
    assert captured['env']['PYTHONPATH'].split(desktop_runtime_setup.os.pathsep)[:2] == [
        str(PYTHON_MODULE_DIR),
        str(Path(__file__).resolve().parents[2]),
    ]
    assert captured['env']['TOKEN_PLACE_DESKTOP_BOOTSTRAP_SCRIPT'].endswith(
        'desktop_runtime_setup.py'
    )


def test_probe_falls_back_when_payload_is_not_json(monkeypatch):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)

    class _Result:
        returncode = 0
        stdout = 'not-json'
        stderr = 'json parse failed'

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _Result())

    probe = desktop_runtime_setup._probe_llama_runtime()
    assert probe.backend == 'missing'
    assert probe.error == 'json parse failed'


def test_is_repo_local_llama_module_returns_false_for_empty_module_path():
    assert desktop_runtime_setup._is_repo_local_llama_module('', Path.cwd()) is False


def test_is_repo_local_llama_module_returns_false_on_resolve_oserror(monkeypatch):
    class _BrokenPath:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self):
            raise OSError('resolve failed')

    monkeypatch.setattr(desktop_runtime_setup, 'Path', _BrokenPath)
    assert desktop_runtime_setup._is_repo_local_llama_module('C:/llama_cpp.py', Path.cwd()) is False


def test_runtime_bootstrap_fails_fast_when_repo_local_llama_shim_is_detected(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    repo_root = tmp_path / 'repo'
    repo_root.mkdir(parents=True)
    (repo_root / 'llama_cpp.py').write_text('# shim\n', encoding='utf-8')
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_probe_llama_runtime',
        lambda: desktop_runtime_setup.RuntimeProbe(
            backend='cpu',
            gpu_offload_supported=False,
            detected_device='cpu',
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path=str((repo_root / 'llama_cpp.py').resolve()),
            error=None,
        ),
    )

    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=repo_root)

    assert result['runtime_action'] == 'shadowed_repo_llama_cpp'
    assert 'repo-local shim' in result['fallback_reason']


def test_run_pip_install_success_failure_and_timeout(monkeypatch):
    class _OkResult:
        returncode = 0
        stdout = 'ok output'
        stderr = ''

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _OkResult())
    ok, output = desktop_runtime_setup._run_pip_install(['python'], {})
    assert ok is True
    assert output == 'ok output'

    class _FailResult:
        returncode = 1
        stdout = 'fallback stdout'
        stderr = 'real stderr'

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', lambda *args, **kwargs: _FailResult())
    ok, output = desktop_runtime_setup._run_pip_install(['python'], {})
    assert ok is False
    assert output == 'real stderr'

    def _timeout(*_args, **_kwargs):
        raise desktop_runtime_setup.subprocess.TimeoutExpired(cmd='pip', timeout=12)

    monkeypatch.setattr(desktop_runtime_setup.subprocess, 'run', _timeout)
    ok, output = desktop_runtime_setup._run_pip_install(['python'], {}, timeout_seconds=12)
    assert ok is False
    assert output == 'pip install timed out after 12s'


def test_runtime_state_tracks_and_clears_source_repair_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    state_path = tmp_path / 'runtime_state.json'
    monkeypatch.setattr(desktop_runtime_setup, '_runtime_state_path', lambda: state_path)
    now = 2_000.0
    monkeypatch.setattr(desktop_runtime_setup.time, 'time', lambda: now)

    desktop_runtime_setup._record_source_repair_failure('build failed badly')
    can_retry, reason = desktop_runtime_setup._should_attempt_source_repair()
    assert can_retry is False
    assert reason == 'build failed badly'

    desktop_runtime_setup._clear_source_repair_failure()
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert sys.executable not in state.get('source_repair_failures', {})
    monkeypatch.setattr(
        desktop_runtime_setup.time,
        'time',
        lambda: now + desktop_runtime_setup.SOURCE_REPAIR_COOLDOWN_SECONDS + 1,
    )
    can_retry, reason = desktop_runtime_setup._should_attempt_source_repair()
    assert can_retry is True
    assert reason == ''


def test_windows_packaged_layout_without_requirements_falls_back_without_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_runtime_setup, 'sys', _SysStub)
    monkeypatch.setattr(desktop_runtime_setup, '_probe_llama_runtime', lambda: _probe())
    monkeypatch.setattr(desktop_runtime_setup, '_should_attempt_source_repair', lambda: (True, ''))
    monkeypatch.setattr(desktop_runtime_setup, '_record_source_repair_failure', lambda _reason: None)
    monkeypatch.setattr(
        desktop_runtime_setup,
        '_run_pip_install',
        lambda *_args, **_kwargs: (False, 'simulated pip source build failure'),
    )
    monkeypatch.setattr(desktop_runtime_setup, '_fallback_unpinned_plans', lambda _platform: [])
    monkeypatch.setattr(desktop_runtime_setup, 'llama_cpp_install_plan_fallbacks', lambda **_kwargs: [])

    packaged_root = tmp_path / 'Users' / 'danie' / 'AppData' / 'Local' / 'token.place'
    packaged_root.mkdir(parents=True)
    result = desktop_runtime_setup.ensure_desktop_llama_runtime('auto', repo_root=packaged_root)

    assert result['runtime_action'] == 'failed'
    assert result['selected_backend'] == 'cpu'
    assert '[Errno 2]' not in result['fallback_reason']
    assert 'requirements file not found' in result['fallback_reason']
    assert 'falling back to unpinned llama-cpp-python source reinstall' in result['fallback_reason']


def test_is_repo_local_llama_module_uses_case_insensitive_comparison(tmp_path):
    repo_root = tmp_path / 'RepoRoot'
    repo_root.mkdir(parents=True)
    shim = repo_root / 'llama_cpp.py'
    shim.write_text('# shim\n', encoding='utf-8')

    module_path = str(shim.resolve()).upper()
    assert desktop_runtime_setup._is_repo_local_llama_module(module_path, repo_root) is True
