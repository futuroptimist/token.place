"""Contract tests for the Windows NVIDIA desktop smoke-test helper."""

from __future__ import annotations

import argparse
import importlib.util
import json
import queue
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / 'desktop-tauri'
    / 'scripts'
    / 'windows_nvidia_gpu_smoke_test.py'
)
SPEC = importlib.util.spec_from_file_location('windows_nvidia_gpu_smoke_test', MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None, (
    f'Could not load {MODULE_PATH} — ensure desktop-tauri/scripts/ is present in the repo'
)
windows_gpu_smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(windows_gpu_smoke)


def _make_repo_root_with_bootstrap(tmp_path: Path) -> Path:
    repo_root = tmp_path / 'repo'
    python_root = repo_root / 'desktop-tauri' / 'src-tauri' / 'python'
    python_root.mkdir(parents=True)
    (python_root / 'path_bootstrap.py').write_text(
        'def ensure_runtime_import_paths(*_args, **_kwargs):\n'
        '    return None\n',
        encoding='utf-8',
    )
    (repo_root / 'llama_cpp.py').write_text('# shim\n', encoding='utf-8')
    return repo_root


def test_main_accepts_runtime_reexec_and_cuda_bridge_started(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('stub', encoding='utf-8')
    repo_root = _make_repo_root_with_bootstrap(tmp_path)

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(model=str(model_path), mode='auto', context_tier='64k-full'),
    )
    monkeypatch.setattr(windows_gpu_smoke.os.path, 'exists', lambda _path: True)
    monkeypatch.setattr(windows_gpu_smoke, '_load_compute_runtime_diagnostics', lambda *_args: {
        'backend_available': 'cuda',
        'backend_used': 'cuda',
        'offloaded_layers': 40,
        'kv_cache_device': 'cuda',
        'fallback_reason': '',
        'runtime_setup': {
            'runtime_action': 'installed_cuda_reexec',
            'interpreter': 'C:/Python312/python.exe',
        },
    })
    monkeypatch.setattr(windows_gpu_smoke, '_run_bridge_oneshot', lambda *_args: (
        {
            'type': 'started',
            'backend_available': 'cuda',
            'backend_used': 'cuda',
            'offloaded_layers': 32,
            'kv_cache_device': 'cuda',
            'interpreter': 'C:/Python312/python.exe',
            'llama_repo_stub_imported': False,
            'warm_load_state': 'ready',
            'registered': True,
            'context_tier': '64k-full',
        },
        [{'type': 'started'}],
        '',
    ))

    status = windows_gpu_smoke.main()
    assert status == 0


def test_main_fails_when_bridge_reports_cpu_fallback(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('stub', encoding='utf-8')
    repo_root = _make_repo_root_with_bootstrap(tmp_path)

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(model=str(model_path), mode='gpu', context_tier='64k-full'),
    )
    monkeypatch.setattr(windows_gpu_smoke.os.path, 'exists', lambda _path: True)
    monkeypatch.setattr(windows_gpu_smoke, '_load_compute_runtime_diagnostics', lambda *_args: {
        'backend_available': 'cuda',
        'backend_used': 'cuda',
        'offloaded_layers': 16,
        'kv_cache_device': 'cuda',
        'fallback_reason': '',
        'runtime_setup': {
            'runtime_action': 'already_supported',
            'interpreter': 'C:/Python312/python.exe',
        },
    })
    monkeypatch.setattr(windows_gpu_smoke, '_run_bridge_oneshot', lambda *_args: (
        {
            'type': 'started',
            'backend_available': 'cpu',
            'backend_used': 'cpu',
            'offloaded_layers': 0,
            'kv_cache_device': 'cpu',
            'interpreter': 'C:/Python312/python.exe',
            'llama_repo_stub_imported': False,
            'warm_load_state': 'ready',
            'registered': True,
            'context_tier': '64k-full',
        },
        [{'type': 'started'}],
        '',
    ))

    status = windows_gpu_smoke.main()
    assert status == 1

class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, stdout_text: str, stderr_text: str = '') -> None:
        import io

        self.stdin = _FakeStdin()
        self.stdout = io.StringIO(stdout_text)
        self.stderr = io.StringIO(stderr_text)
        self.returncode = None
        self.pid = 12345
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9



def test_load_compute_runtime_diagnostics_forwards_context_tier(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    calls: dict[str, object] = {}

    fake_manager = SimpleNamespace(model_path=None)

    def fake_ensure(mode, *, context_tier):
        calls['runtime_setup'] = (mode, context_tier)
        return {'runtime_action': 'already_supported'}

    def fake_apply_mode(manager, mode):
        calls['mode'] = (manager, mode)

    def fake_apply_context(manager, context_tier):
        calls['context'] = (manager, context_tier)
        manager.context_tier = context_tier

    def fake_get_manager():
        calls['manager_requested'] = True
        return fake_manager

    def fake_compute_diagnostics(manager):
        calls['diagnostics_manager'] = manager
        return {'context_tier': getattr(manager, 'context_tier', None)}

    fake_manager.get_llm_instance = lambda: calls.setdefault('loaded_after_context', fake_manager.context_tier)

    monkeypatch.setitem(
        sys.modules,
        'desktop_runtime_setup',
        SimpleNamespace(ENABLE_BOOTSTRAP_ENV='TOKEN_PLACE_DESKTOP_RUNTIME_BOOTSTRAP', ensure_desktop_llama_runtime=fake_ensure),
    )
    monkeypatch.setitem(
        sys.modules,
        'utils.compute_node_runtime',
        SimpleNamespace(apply_compute_mode=fake_apply_mode, compute_mode_diagnostics=fake_compute_diagnostics),
    )
    monkeypatch.setitem(
        sys.modules,
        'utils.context_profiles',
        SimpleNamespace(apply_context_profile=fake_apply_context),
    )
    monkeypatch.setitem(
        sys.modules,
        'utils.llm.model_manager',
        SimpleNamespace(get_model_manager=fake_get_manager),
    )

    diagnostics = windows_gpu_smoke._load_compute_runtime_diagnostics(str(model_path), 'gpu', '64k-full')

    assert calls['runtime_setup'] == ('gpu', '64k-full')
    assert calls['mode'] == (fake_manager, 'gpu')
    assert calls['context'] == (fake_manager, '64k-full')
    assert calls['loaded_after_context'] == '64k-full'
    assert diagnostics['context_tier'] == '64k-full'
    assert diagnostics['runtime_setup'] == {'runtime_action': 'already_supported'}

def test_run_bridge_ignores_provisioning_started_and_cancels_after_ready(monkeypatch, tmp_path):
    repo_root = _make_repo_root_with_bootstrap(tmp_path)
    created: list[_FakeProcess] = []
    provisioning = {
        'type': 'started',
        'running': True,
        'registered': False,
        'worker_state': 'provisioning',
        'backend_available': 'pending',
        'backend_used': 'pending',
        'context_tier': '64k-full',
    }
    ready = {
        'type': 'started',
        'running': True,
        'registered': True,
        'worker_state': 'ready',
        'warm_load_state': 'ready',
        'backend_available': 'cuda',
        'backend_used': 'cuda',
        'offloaded_layers': 40,
        'kv_cache_device': 'cuda',
        'llama_repo_stub_imported': False,
        'context_tier': '64k-full',
    }

    def fake_popen(*_args, **_kwargs):
        proc = _FakeProcess('\n'.join([json.dumps(provisioning), json.dumps(ready), '']))
        created.append(proc)
        return proc

    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'Popen', fake_popen)

    started, events, _stderr = windows_gpu_smoke._run_bridge_oneshot('model.gguf', 'gpu', '64k-full')

    assert started == ready
    assert events == [provisioning, ready]
    assert created[0].stdin.writes == ['{"type":"cancel"}\n']
    assert created[0].stdin.closed is True


def test_run_bridge_fails_and_reaps_on_provisioning_then_error(monkeypatch, tmp_path):
    repo_root = _make_repo_root_with_bootstrap(tmp_path)
    created: list[_FakeProcess] = []
    provisioning = {'type': 'started', 'worker_state': 'provisioning', 'backend_available': 'pending'}
    error = {'type': 'error', 'message': 'boom'}

    def fake_popen(*_args, **_kwargs):
        proc = _FakeProcess('\n'.join([json.dumps(provisioning), json.dumps(error), '']))
        created.append(proc)
        return proc

    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(windows_gpu_smoke, '_terminate_process_tree', lambda proc: proc.terminate())

    try:
        windows_gpu_smoke._run_bridge_oneshot('model.gguf', 'gpu', '64k-full')
    except RuntimeError as exc:
        assert 'bridge validation failed' in str(exc)
    else:
        raise AssertionError('expected bridge validation failure')
    assert created[0].terminated is True
    assert created[0].stdin.writes == []


def test_main_forwards_context_tier_to_runtime_and_bridge(monkeypatch, tmp_path):
    model_path = tmp_path / 'model.gguf'
    model_path.write_text('stub', encoding='utf-8')
    repo_root = _make_repo_root_with_bootstrap(tmp_path)
    calls: dict[str, tuple[str, str, str] | tuple[str, str, str]] = {}

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(model=str(model_path), mode='gpu', context_tier='64k-full'),
    )
    monkeypatch.setattr(windows_gpu_smoke.os.path, 'exists', lambda _path: True)

    def fake_diagnostics(model, mode, context_tier):
        calls['diagnostics'] = (model, mode, context_tier)
        return {
            'backend_available': 'cuda',
            'backend_used': 'cuda',
            'offloaded_layers': 16,
            'kv_cache_device': 'cuda',
            'runtime_setup': {'runtime_action': 'already_supported', 'interpreter': 'python'},
        }

    def fake_bridge(model, mode, context_tier):
        calls['bridge'] = (model, mode, context_tier)
        return ({
            'type': 'started',
            'registered': True,
            'warm_load_state': 'ready',
            'backend_available': 'cuda',
            'backend_used': 'cuda',
            'offloaded_layers': 16,
            'kv_cache_device': 'cuda',
            'llama_repo_stub_imported': False,
            'context_tier': context_tier,
            'interpreter': 'python',
        }, [], '')

    monkeypatch.setattr(windows_gpu_smoke, '_load_compute_runtime_diagnostics', fake_diagnostics)
    monkeypatch.setattr(windows_gpu_smoke, '_run_bridge_oneshot', fake_bridge)

    assert windows_gpu_smoke.main() == 0
    assert calls['diagnostics'] == (str(model_path), 'gpu', '64k-full')
    assert calls['bridge'] == (str(model_path), 'gpu', '64k-full')


def test_launch_materialized_child_materializes_once_reexecs_canonical_and_cleans(monkeypatch, tmp_path):
    installer = tmp_path / 'Token.Place_0.1.2_x64-setup.exe'
    installer.write_text('installer', encoding='utf-8')
    materialized: list[Path] = []
    cleaned: list[Path] = []
    runs: list[dict[str, object]] = []

    def fake_materialize(path, install_root):
        assert path == installer
        materialized.append(install_root)
        runtime = install_root / 'resources' / 'python-runtime'
        runtime.mkdir(parents=True)
        (runtime / 'python.exe').write_text('python', encoding='utf-8')
        bridge_root = install_root / 'resources' / 'python'
        bridge_root.mkdir(parents=True)
        (bridge_root / 'compute_node_bridge.py').write_text('# bridge', encoding='utf-8')

    class Result:
        returncode = 7

    def fake_run(cmd, **kwargs):
        runs.append({'cmd': cmd, **kwargs})
        return Result()

    monkeypatch.setattr(windows_gpu_smoke, '_materialize_release_artifact', fake_materialize)
    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'run', fake_run)
    monkeypatch.setattr(windows_gpu_smoke, '_run_nsis_uninstaller_once', lambda root: cleaned.append(root))

    args = argparse.Namespace(installer=installer, model=tmp_path / 'model.gguf', mode='gpu', context_tier='64k-full')
    assert windows_gpu_smoke._launch_materialized_child(args) == 7

    assert len(materialized) == 1
    assert cleaned == materialized
    assert len(runs) == 1
    cmd = runs[0]['cmd']
    assert str(cmd[0]).endswith('python.exe')
    assert '--python-exe' in cmd and '--resource-root' in cmd
    assert '--installer' not in cmd and '--artifact-root' not in cmd
    assert '--mode' in cmd and cmd[cmd.index('--mode') + 1] == 'gpu'
    assert '--context-tier' in cmd and cmd[cmd.index('--context-tier') + 1] == '64k-full'
    assert runs[0]['cwd'].endswith('resources')
    env = runs[0]['env']
    assert env['TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP'] == '1'
    assert 'TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET' not in env


def test_materialize_release_artifact_uses_format_specific_windows_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs['check'] is True
        assert kwargs['stdout'] is windows_gpu_smoke.subprocess.PIPE
        assert kwargs['stderr'] is windows_gpu_smoke.subprocess.STDOUT
        assert kwargs['text'] is True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'run', fake_run)
    msi = tmp_path / 'app.msi'
    exe = tmp_path / 'app-setup.exe'
    msi.write_text('msi', encoding='utf-8')
    exe.write_text('exe', encoding='utf-8')

    windows_gpu_smoke._materialize_release_artifact(msi, tmp_path / 'msi-root')
    windows_gpu_smoke._materialize_release_artifact(exe, tmp_path / 'exe-root')

    assert calls[0][0] == 'msiexec.exe'
    assert calls[0][1:4] == ['/a', str(msi.resolve()), '/qn']
    assert calls[0][4] == '/norestart'
    assert calls[0][5].startswith('TARGETDIR=')
    assert calls[1][0] == str(exe.resolve())
    assert calls[1][1] == '/S'
    assert calls[1][2].startswith('/D=')


def test_reexec_with_bundled_python_sanitizes_env_and_skips_when_current(monkeypatch, tmp_path):
    resource_root = tmp_path / 'resources'
    python_exe = resource_root / 'python-runtime' / 'python.exe'
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text('python', encoding='utf-8')
    monkeypatch.setenv('TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', 'C:/Users/Secret/site')
    exec_calls: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(windows_gpu_smoke.sys, 'executable', str(tmp_path / 'host-python.exe'))
    monkeypatch.setattr(windows_gpu_smoke.os, 'execve', lambda exe, argv, env: exec_calls.append((exe, argv, env)))

    windows_gpu_smoke._maybe_reexec_with_bundled_python(python_exe, resource_root, ['--mode', 'gpu'])

    assert len(exec_calls) == 1
    exe, argv, env = exec_calls[0]
    assert exe == str(python_exe)
    assert argv[0] == str(python_exe)
    assert '--mode' in argv
    assert 'TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET' not in env
    assert env['TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP'] == '1'

    exec_calls.clear()
    monkeypatch.setattr(windows_gpu_smoke.sys, 'executable', str(python_exe))
    windows_gpu_smoke._maybe_reexec_with_bundled_python(python_exe, resource_root, [])
    assert exec_calls == []


def test_helper_failure_and_runtime_discovery_edges(monkeypatch, tmp_path, capsys):
    assert windows_gpu_smoke._fail('boom') == 1
    assert 'FAIL: boom' in capsys.readouterr().err
    try:
        windows_gpu_smoke._require(False, 'missing')
    except RuntimeError as exc:
        assert str(exc) == 'missing'
    else:
        raise AssertionError('expected failed requirement')

    root = tmp_path / 'install'
    runtime = root / 'app' / 'python-runtime'
    bridge = root / 'app' / 'python'
    runtime.mkdir(parents=True)
    bridge.mkdir(parents=True)
    (runtime / 'python.exe').write_text('python', encoding='utf-8')
    (bridge / 'compute_node_bridge.py').write_text('# bridge', encoding='utf-8')
    assert windows_gpu_smoke._find_materialized_runtime(root) == (runtime / 'python.exe', root / 'app')

    (root / 'second' / 'python-runtime').mkdir(parents=True)
    (root / 'second' / 'python-runtime' / 'python.exe').write_text('python', encoding='utf-8')
    try:
        windows_gpu_smoke._find_materialized_runtime(root)
    except RuntimeError as exc:
        assert 'expected exactly one bundled' in str(exc)
    else:
        raise AssertionError('expected duplicate runtime failure')


def test_materialize_release_artifact_directory_and_fail_closed_edges(monkeypatch, tmp_path):
    source = tmp_path / 'prematerialized'
    (source / 'python-runtime').mkdir(parents=True)
    (source / 'python-runtime' / 'python.exe').write_text('python', encoding='utf-8')
    dest = tmp_path / 'dest'
    windows_gpu_smoke._materialize_release_artifact(source, dest)
    assert (dest / 'python-runtime' / 'python.exe').is_file()

    installer = tmp_path / 'app.pkg'
    installer.write_text('pkg', encoding='utf-8')
    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'linux')
    try:
        windows_gpu_smoke._materialize_release_artifact(installer, tmp_path / 'out')
    except RuntimeError as exc:
        assert 'requires Windows' in str(exc)
    else:
        raise AssertionError('expected non-Windows materialization failure')

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    try:
        windows_gpu_smoke._materialize_release_artifact(installer, tmp_path / 'out')
    except RuntimeError as exc:
        assert 'unsupported Windows installer artifact' in str(exc)
    else:
        raise AssertionError('expected unsupported installer failure')


def test_runtime_ready_predicate_and_layer_normalization_edges():
    assert windows_gpu_smoke._offloaded_layer_count('all_supported_layers') == 1
    assert windows_gpu_smoke._offloaded_layer_count(3) == 3
    base = {
        'type': 'started',
        'registered': True,
        'context_tier': '64k-full',
        'warm_load_state': 'ready',
        'backend_available': 'cuda',
        'backend_used': 'cuda',
        'llama_repo_stub_imported': False,
        'offloaded_layers': 'all_supported_layers',
        'kv_cache_device': 'cuda:0',
    }
    assert windows_gpu_smoke._is_truthy_cuda_ready(dict(base), '64k-full') is True
    for key, value in [
        ('type', 'status'),
        ('worker_state', 'provisioning'),
        ('backend_available', 'pending'),
        ('registered', False),
        ('context_tier', '8k-fast'),
        ('warm_load_state', 'loading'),
        ('backend_used', 'cpu'),
        ('llama_repo_stub_imported', True),
        ('offloaded_layers', 0),
        ('kv_cache_device', 'cpu'),
    ]:
        event = dict(base)
        event[key] = value
        assert windows_gpu_smoke._is_truthy_cuda_ready(event, '64k-full') is False


def test_terminate_process_tree_uses_posix_group_then_kill(monkeypatch):
    calls: list[tuple[str, int]] = []

    class SlowProcess:
        pid = 4242
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if timeout == 5 and len(calls) == 1:
                raise windows_gpu_smoke.subprocess.TimeoutExpired('proc', timeout)
            self.returncode = -9
            return self.returncode

        def terminate(self):
            calls.append(('terminate', self.pid))

        def kill(self):
            calls.append(('kill', self.pid))

    monkeypatch.setattr(windows_gpu_smoke.os, 'name', 'posix', raising=False)
    monkeypatch.setattr(windows_gpu_smoke.os, 'killpg', lambda pid, sig: calls.append((str(sig), pid)))

    windows_gpu_smoke._terminate_process_tree(SlowProcess())
    assert calls[0][1] == 4242
    assert len(calls) >= 2


def test_drain_lines_collects_tail_and_queue():
    import io

    output = queue.Queue()
    tail: deque[str] = deque(maxlen=3)
    windows_gpu_smoke._drain_lines(io.StringIO('one\ntwo\n'), output, tail)
    assert list(tail) == ['one', 'two']
    assert output.get_nowait() == 'one\n'
    assert output.get_nowait() == 'two\n'


def test_main_rejects_non_windows_and_missing_model(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'linux')
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(
            model=str(tmp_path / 'missing.gguf'),
            mode='gpu',
            context_tier='64k-full',
            resource_root=None,
            python_exe=None,
            artifact_root=None,
            installer=None,
        ),
    )
    assert windows_gpu_smoke.main() == 1

    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    assert windows_gpu_smoke.main() == 1


def test_nsis_uninstaller_noop_and_invocation(monkeypatch, tmp_path):
    install_root = tmp_path / 'install'
    install_root.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'run', lambda cmd, **_kwargs: calls.append(cmd))

    windows_gpu_smoke._run_nsis_uninstaller_once(install_root)
    assert calls == []

    nested = install_root / 'app'
    nested.mkdir()
    uninstaller = nested / 'uninstall.exe'
    uninstaller.write_text('uninstall', encoding='utf-8')
    windows_gpu_smoke._run_nsis_uninstaller_once(install_root)
    assert calls == [[str(uninstaller), '/S']]


def test_launch_materialized_child_cleanup_failure_propagates(monkeypatch, tmp_path):
    installer = tmp_path / 'Token.Place_0.1.2_x64-setup.exe'
    installer.write_text('installer', encoding='utf-8')

    def fake_materialize(_path, install_root):
        runtime = install_root / 'resources' / 'python-runtime'
        runtime.mkdir(parents=True)
        (runtime / 'python.exe').write_text('python', encoding='utf-8')
        bridge_root = install_root / 'resources' / 'python'
        bridge_root.mkdir(parents=True)
        (bridge_root / 'compute_node_bridge.py').write_text('# bridge', encoding='utf-8')

    monkeypatch.setattr(windows_gpu_smoke, '_materialize_release_artifact', fake_materialize)
    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'run', lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(
        windows_gpu_smoke,
        '_run_nsis_uninstaller_once',
        lambda _root: (_ for _ in ()).throw(RuntimeError('cleanup failed')),
    )

    args = argparse.Namespace(installer=installer, model=tmp_path / 'model.gguf', mode='gpu', context_tier='64k-full')
    try:
        windows_gpu_smoke._launch_materialized_child(args)
    except RuntimeError as exc:
        assert 'cleanup failed' in str(exc)
    else:
        raise AssertionError('expected cleanup failure to propagate')


def test_reexec_missing_interpreter_and_resolve_oserror(monkeypatch, tmp_path):
    resource_root = tmp_path / 'resources'
    missing_python = resource_root / 'python-runtime' / 'python.exe'
    monkeypatch.setattr(windows_gpu_smoke.sys, 'executable', str(tmp_path / 'host.exe'))
    try:
        windows_gpu_smoke._maybe_reexec_with_bundled_python(missing_python, resource_root, [])
    except RuntimeError as exc:
        assert 'bundled interpreter is missing' in str(exc)
    else:
        raise AssertionError('expected missing interpreter failure')

    python_exe = resource_root / 'python-runtime' / 'python.exe'
    python_exe.parent.mkdir(parents=True)
    python_exe.write_text('python', encoding='utf-8')
    exec_calls: list[tuple[str, list[str], dict[str, str]]] = []

    class BrokenPath(type(python_exe)):
        def resolve(self):  # type: ignore[override]
            raise OSError('bad path')

    broken_python = BrokenPath(python_exe)
    monkeypatch.setattr(windows_gpu_smoke.os, 'execve', lambda exe, argv, env: exec_calls.append((exe, argv, env)))
    windows_gpu_smoke._maybe_reexec_with_bundled_python(broken_python, resource_root, ['--mode', 'gpu'])
    assert exec_calls and exec_calls[0][0] == str(python_exe)


def test_run_bridge_oneshot_timeout_exit_and_json_edges(monkeypatch, tmp_path):
    repo_root = _make_repo_root_with_bootstrap(tmp_path)
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(windows_gpu_smoke, 'BRIDGE_TIMEOUT_SECONDS', 0.01)
    terminated: list[object] = []
    monkeypatch.setattr(windows_gpu_smoke, '_terminate_process_tree', lambda proc: terminated.append(proc))

    class ExitedProcess(_FakeProcess):
        def poll(self):
            return 1

    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'Popen', lambda *_args, **_kwargs: ExitedProcess('not-json\n[]\n'))
    try:
        windows_gpu_smoke._run_bridge_oneshot('model.gguf', 'gpu', '64k-full')
    except RuntimeError as exc:
        text = str(exc)
        assert 'exited before emitting' in text or 'bridge validation failed' in text
    else:
        raise AssertionError('expected exited-before-ready failure')
    assert terminated

    class IdleProcess(_FakeProcess):
        def __init__(self):
            super().__init__('')

        def poll(self):
            return None

    terminated.clear()
    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'Popen', lambda *_args, **_kwargs: IdleProcess())
    try:
        windows_gpu_smoke._run_bridge_oneshot('model.gguf', 'gpu', '64k-full')
    except RuntimeError as exc:
        assert 'bridge validation failed' in str(exc)
    else:
        raise AssertionError('expected timeout failure')
    assert terminated


def test_run_bridge_oneshot_resource_root_uses_bundled_environment(monkeypatch, tmp_path):
    resource_root = tmp_path / 'resources'
    python_root = resource_root / 'python'
    python_root.mkdir(parents=True)
    (python_root / 'compute_node_bridge.py').write_text('# bridge', encoding='utf-8')
    popen_kwargs: dict[str, object] = {}
    ready = {
        'type': 'started',
        'registered': True,
        'warm_load_state': 'ready',
        'backend_available': 'cuda',
        'backend_used': 'cuda',
        'offloaded_layers': 2,
        'kv_cache_device': 'cuda:0',
        'llama_repo_stub_imported': False,
        'context_tier': '64k-full',
    }

    def fake_popen(*_args, **kwargs):
        popen_kwargs.update(kwargs)
        return _FakeProcess(json.dumps(ready) + '\n')

    monkeypatch.setattr(windows_gpu_smoke.subprocess, 'Popen', fake_popen)
    started, _events, _stderr = windows_gpu_smoke._run_bridge_oneshot('model.gguf', 'gpu', '64k-full', resource_root)

    assert started == ready
    assert popen_kwargs['cwd'] == str(resource_root)
    env = popen_kwargs['env']
    assert env['PYTHONPATH'] == str(python_root)
    assert env['TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP'] == '1'


def test_main_installer_handoff_and_runtime_requirement_failures(monkeypatch, tmp_path):
    installer = tmp_path / 'app-setup.exe'
    installer.write_text('installer', encoding='utf-8')
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(
            model=str(tmp_path / 'model.gguf'),
            mode='gpu',
            context_tier='64k-full',
            resource_root=None,
            python_exe=None,
            artifact_root=None,
            installer=installer,
        ),
    )
    monkeypatch.setattr(windows_gpu_smoke, '_launch_materialized_child', lambda args: 23)
    assert windows_gpu_smoke.main() == 23

    model_path = tmp_path / 'model.gguf'
    model_path.write_text('model', encoding='utf-8')
    monkeypatch.setattr(windows_gpu_smoke.sys, 'platform', 'win32')
    monkeypatch.setattr(
        argparse.ArgumentParser,
        'parse_args',
        lambda _self: argparse.Namespace(
            model=str(model_path),
            mode='gpu',
            context_tier='64k-full',
            resource_root=None,
            python_exe=None,
            artifact_root=None,
            installer=None,
        ),
    )
    monkeypatch.setattr(windows_gpu_smoke.os.path, 'exists', lambda _path: True)
    repo_root = _make_repo_root_with_bootstrap(tmp_path)
    monkeypatch.setattr(windows_gpu_smoke, '_repo_root', lambda: repo_root)
    monkeypatch.setattr(windows_gpu_smoke, '_run_bridge_oneshot', lambda *_args: ({}, [], ''))

    for diagnostics in [
        {'backend_available': 'cpu', 'backend_used': 'cuda', 'offloaded_layers': 1, 'kv_cache_device': 'cuda', 'runtime_setup': {'runtime_action': 'already_supported'}},
        {'backend_available': 'cuda', 'backend_used': 'cuda', 'offloaded_layers': 0, 'kv_cache_device': 'cuda', 'runtime_setup': {'runtime_action': 'already_supported'}},
        {'backend_available': 'cuda', 'backend_used': 'cuda', 'offloaded_layers': 1, 'kv_cache_device': 'cpu', 'runtime_setup': {'runtime_action': 'already_supported'}},
        {'backend_available': 'cuda', 'backend_used': 'cuda', 'offloaded_layers': 1, 'kv_cache_device': 'cuda', 'runtime_setup': {'runtime_action': 'shadowed_repo_llama_cpp'}},
    ]:
        monkeypatch.setattr(windows_gpu_smoke, '_load_compute_runtime_diagnostics', lambda *_args, d=diagnostics: d)
        assert windows_gpu_smoke.main() == 1
