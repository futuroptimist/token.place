#!/usr/bin/env python3
"""Manual Windows + NVIDIA smoke test for desktop sidecar GPU runtime viability."""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

BRIDGE_TIMEOUT_SECONDS = 180.0
DIAGNOSTIC_TAIL_LINES = 80


def _fail(message: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    return 1


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sanitize_env_for_bundled_runtime(resource_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        upper = key.upper()
        if (
            upper.startswith('PIP_')
            or upper.startswith('CMAKE_')
            or upper in {'PYTHONHOME', 'TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET', 'TOKEN_PLACE_DESKTOP_DEV_ALLOW_SOURCE_BUILD', 'TOKEN_PLACE_DESKTOP_ENABLE_RUNTIME_BOOTSTRAP', 'FORCE_CMAKE'}
        ):
            env.pop(key, None)
    python_root = resource_root / 'python'
    env['PYTHONPATH'] = str(python_root)
    env['PYTHONNOUSERSITE'] = '1'
    env['TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP'] = '1'
    return env


def _find_materialized_runtime(extract_root: Path) -> tuple[Path, Path]:
    candidates = [p for p in extract_root.rglob('python.exe') if p.parent.name.lower() == 'python-runtime']
    if len(candidates) != 1:
        raise RuntimeError(f'expected exactly one bundled python-runtime/python.exe in release artifact, found {len(candidates)}')
    python_exe = candidates[0]
    resources = python_exe.parent.parent
    if not (resources / 'python' / 'compute_node_bridge.py').is_file():
        bridges = list(extract_root.rglob('compute_node_bridge.py'))
        if not bridges:
            raise RuntimeError('release artifact resources are missing compute_node_bridge.py')
        resources = bridges[0].parent.parent
    return python_exe, resources


def _materialize_release_artifact(installer: Path, extract_root: Path) -> None:
    if installer.is_dir():
        shutil.copytree(installer, extract_root, dirs_exist_ok=True)
        return
    if not sys.platform.startswith('win'):
        raise RuntimeError('Windows installer materialization requires Windows')
    installer_abs = installer.resolve()
    root_abs = extract_root.resolve()
    suffix = installer.suffix.lower()
    if suffix == '.msi':
        subprocess.run(['msiexec.exe', '/a', str(installer_abs), '/qn', '/norestart', f'TARGETDIR={root_abs}'], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return
    if suffix == '.exe':
        subprocess.run([str(installer_abs), '/S', f'/D={root_abs}'], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return
    raise RuntimeError(f'unsupported Windows installer artifact: {installer.name}')


def _canonical_child_args(args: argparse.Namespace, python_exe: Path, resource_root: Path) -> list[str]:
    child = ['--python-exe', str(python_exe), '--resource-root', str(resource_root)]
    if args.model:
        child.extend(['--model', str(args.model)])
    child.extend(['--mode', args.mode, '--context-tier', args.context_tier])
    return child


def _launch_materialized_child(args: argparse.Namespace) -> int:
    install_root = Path(tempfile.mkdtemp(prefix='token-place-win-smoke-'))
    try:
        _materialize_release_artifact(args.installer, install_root)
        python_exe, resource_root = _find_materialized_runtime(install_root)
        child_args = _canonical_child_args(args, python_exe, resource_root)
        env = _sanitize_env_for_bundled_runtime(resource_root)
        completed = subprocess.run([str(python_exe), str(Path(__file__).resolve()), *child_args], env=env, cwd=str(resource_root), check=False)
        return int(completed.returncode)
    finally:
        shutil.rmtree(install_root, ignore_errors=True)


def _maybe_reexec_with_bundled_python(python_exe: Path | None, resource_root: Path | None, argv: list[str]) -> None:
    if python_exe is None:
        return
    try:
        current = Path(sys.executable).resolve()
        target = python_exe.resolve()
    except OSError:
        target = python_exe
        current = Path(sys.executable)
    if current == target:
        return
    if not python_exe.is_file():
        raise RuntimeError('bundled interpreter is missing from release artifact')
    env = _sanitize_env_for_bundled_runtime(resource_root or python_exe.parent.parent)
    os.execve(str(python_exe), [str(python_exe), str(Path(__file__).resolve()), *argv], env)


def _offloaded_layer_count(value: Any) -> int:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == 'all_supported_layers':
            return 1
    return int(value or 0)


def _is_truthy_cuda_ready(event: dict[str, Any], context_tier: str) -> bool:
    """Return True for the bridge's authoritative operational CUDA-ready event."""

    if event.get('type') != 'started':
        return False
    if event.get('worker_state') == 'provisioning':
        return False
    if event.get('backend_available') == 'pending' or event.get('backend_used') == 'pending':
        return False
    if event.get('registered') is not True:
        return False
    if event.get('context_tier') != context_tier:
        return False
    if event.get('warm_load_state') != 'ready':
        return False
    if event.get('backend_available') != 'cuda' or event.get('backend_used') != 'cuda':
        return False
    if event.get('llama_repo_stub_imported') is not False:
        return False
    if _offloaded_layer_count(event.get('offloaded_layers')) <= 0:
        return False
    kv_cache = str(event.get('kv_cache_device') or '').lower()
    return kv_cache not in {'', 'cpu'}


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(
                ['taskkill', '/T', '/F', '/PID', str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if os.name == 'nt':
                subprocess.run(
                    ['taskkill', '/T', '/F', '/PID', str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()
        process.wait(timeout=5)


def _drain_lines(stream: Any, output: 'queue.Queue[str] | None', tail: deque[str]) -> None:
    try:
        for line in iter(stream.readline, ''):
            if not line:
                break
            tail.append(line.rstrip('\n'))
            if output is not None:
                output.put(line)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _load_compute_runtime_diagnostics(model_path: str, mode: str, context_tier: str) -> dict[str, Any]:
    from desktop_runtime_setup import ENABLE_BOOTSTRAP_ENV, ensure_desktop_llama_runtime
    from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
    from utils.context_profiles import apply_context_profile
    from utils.llm.model_manager import get_model_manager

    os.environ.setdefault(ENABLE_BOOTSTRAP_ENV, '1')
    runtime_setup = ensure_desktop_llama_runtime(mode, context_tier=context_tier)

    manager = get_model_manager()
    manager.model_path = model_path
    apply_compute_mode(manager, mode)
    apply_context_profile(manager, context_tier)
    manager.get_llm_instance()
    diagnostics = compute_mode_diagnostics(manager)
    diagnostics['runtime_setup'] = runtime_setup
    return diagnostics


def _run_bridge_oneshot(model_path: str, mode: str, context_tier: str, resource_root: Path | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    repo_root = _repo_root()
    python_root = (resource_root / 'python') if resource_root else (repo_root / 'desktop-tauri' / 'src-tauri' / 'python')
    bridge_path = python_root / 'compute_node_bridge.py'
    if resource_root:
        env = _sanitize_env_for_bundled_runtime(resource_root)
        env['PYTHONPATH'] = str(python_root)
        cwd = str(resource_root)
    else:
        env = os.environ.copy()
        existing_pythonpath = env.get('PYTHONPATH', '')
        entries = [str(repo_root), str(python_root)]
        if existing_pythonpath:
            entries.append(existing_pythonpath)
        env['PYTHONPATH'] = os.pathsep.join(entries)
        cwd = str(repo_root)

    popen_kwargs: dict[str, Any] = {
        'stdin': subprocess.PIPE,
        'stdout': subprocess.PIPE,
        'stderr': subprocess.PIPE,
        'text': True,
        'cwd': cwd,
        'env': env,
    }
    if os.name != 'nt':
        popen_kwargs['start_new_session'] = True

    process = subprocess.Popen(
        [
            sys.executable,
            str(bridge_path),
            '--model',
            model_path,
            '--mode',
            mode,
            '--context-tier',
            context_tier,
            '--relay-url',
            'https://token.place',
        ],
        **popen_kwargs,
    )

    stdout_queue: 'queue.Queue[str]' = queue.Queue()
    stdout_tail: deque[str] = deque(maxlen=DIAGNOSTIC_TAIL_LINES)
    stderr_tail: deque[str] = deque(maxlen=DIAGNOSTIC_TAIL_LINES)
    stdout_thread = threading.Thread(target=_drain_lines, args=(process.stdout, stdout_queue, stdout_tail), daemon=True)
    stderr_thread = threading.Thread(target=_drain_lines, args=(process.stderr, None, stderr_tail), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    events: list[dict[str, Any]] = []
    ready_event: dict[str, Any] = {}
    deadline = time.monotonic() + BRIDGE_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            try:
                line = stdout_queue.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            events.append(event)
            if event.get('type') == 'error':
                raise RuntimeError(f"compute_node_bridge.py emitted error before CUDA readiness: {event.get('message') or event.get('last_error') or event.get('error_code')}")
            if _is_truthy_cuda_ready(event, context_tier):
                ready_event = event
                if process.stdin is not None:
                    process.stdin.write('{"type":"cancel"}\n')
                    process.stdin.flush()
                    process.stdin.close()
                break
        else:
            raise TimeoutError('compute_node_bridge.py did not emit authoritative CUDA-ready started event before timeout')

        if not ready_event:
            raise RuntimeError('compute_node_bridge.py exited before emitting authoritative CUDA-ready started event')
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
        return ready_event, events, '\n'.join(stderr_tail)
    except Exception:
        _terminate_process_tree(process)
        raise RuntimeError(
            'bridge validation failed; '
            f'events_tail={events[-5:]} stderr_tail={list(stderr_tail)[-20:]} stdout_tail={list(stdout_tail)[-20:]}'
        )
    finally:
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except Exception:
                pass
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Validate desktop sidecar GPU path on a Windows NVIDIA machine'
    )
    parser.add_argument('--model', default=os.environ.get('TOKEN_PLACE_WINDOWS_NVIDIA_SMOKE_MODEL'), help='Path to GGUF model used by desktop sidecars')
    parser.add_argument('--mode', default='auto', choices=['auto', 'gpu', 'hybrid'])
    parser.add_argument('--context-tier', default='64k-full', choices=['8k-fast', '64k-full'])
    parser.add_argument('--resource-root', type=Path, help='Extracted release artifact resources root')
    parser.add_argument('--python-exe', type=Path, help='Bundled release artifact python.exe')
    parser.add_argument('--artifact-root', type=Path, help='Directory containing the built Windows artifacts')
    parser.add_argument('--installer', type=Path, help='Built NSIS/MSI artifact to extract for hardware smoke testing')
    args = parser.parse_args()

    try:
        if getattr(args, 'installer', None) and not getattr(args, 'python_exe', None):
            return _launch_materialized_child(args)
        _maybe_reexec_with_bundled_python(getattr(args, 'python_exe', None), getattr(args, 'resource_root', None), sys.argv[1:])
    except Exception as exc:
        return _fail(str(exc))

    if not sys.platform.startswith('win'):
        return _fail('this smoke test is only valid on Windows')
    if not args.model:
        return _fail('model path is required via --model or TOKEN_PLACE_WINDOWS_NVIDIA_SMOKE_MODEL')
    if not os.path.exists(args.model):
        return _fail(f'model path does not exist: {args.model}')

    repo_root = _repo_root()
    resource_root = getattr(args, 'resource_root', None)
    python_root = (resource_root / 'python') if resource_root else (repo_root / 'desktop-tauri' / 'src-tauri' / 'python')
    if str(python_root) not in sys.path:
        sys.path.insert(0, str(python_root))
    if resource_root is None:
        from path_bootstrap import ensure_runtime_import_paths
        ensure_runtime_import_paths(__file__, avoid_llama_cpp_shadowing=True)

    try:
        diagnostics = _load_compute_runtime_diagnostics(args.model, args.mode, args.context_tier)
        runtime_setup = diagnostics.get('runtime_setup', {})
        repo_shim_imported = runtime_setup.get('runtime_action') == 'shadowed_repo_llama_cpp'
        payload = {
            'interpreter': runtime_setup.get('interpreter', sys.executable),
            'repo_shim_imported': repo_shim_imported,
            'backend_available': diagnostics.get('backend_available'),
            'backend_used': diagnostics.get('backend_used'),
            'offloaded_layers': diagnostics.get('offloaded_layers'),
            'kv_cache_device': diagnostics.get('kv_cache_device'),
            'fallback_reason': diagnostics.get('fallback_reason'),
            'context_tier': getattr(diagnostics, 'context_tier', args.context_tier) if not isinstance(diagnostics, dict) else diagnostics.get('context_tier', args.context_tier),
        }
        print(json.dumps(payload, indent=2))

        _require(payload['repo_shim_imported'] is False, 'runtime origin check reported repo-local llama_cpp.py shim')
        _require(str(payload.get('interpreter') or '').strip() != '', 'interpreter is missing')
        _require(payload['backend_available'] == 'cuda', 'backend_available is not cuda')
        _require(payload['backend_used'] == 'cuda', 'backend_used is not cuda')
        _require(_offloaded_layer_count(payload.get('offloaded_layers')) > 0, 'offloaded_layers must be > 0')
        kv_cache = str(payload.get('kv_cache_device') or '').lower()
        _require(kv_cache not in {'', 'cpu'}, 'kv_cache_device indicates CPU-only execution')

        smoke_resource_root = getattr(args, 'resource_root', None)
        if smoke_resource_root is None:
            started, bridge_events, bridge_stderr = _run_bridge_oneshot(args.model, args.mode, args.context_tier)
        else:
            started, bridge_events, bridge_stderr = _run_bridge_oneshot(args.model, args.mode, args.context_tier, smoke_resource_root)
        print(
            json.dumps(
                {
                    'bridge_started': started,
                    'bridge_event_count': len(bridge_events),
                    'bridge_stderr_tail': bridge_stderr[-240:],
                },
                indent=2,
            )
        )
        _require(bool(started), 'compute_node_bridge.py did not emit an authoritative CUDA-ready started event')
        _require(started.get('llama_repo_stub_imported') is False, 'bridge runtime origin check reported repo-local llama_cpp.py shim')
        _require(str(started.get('interpreter') or '').strip() != '', 'bridge started.interpreter is missing')
        _require(started.get('context_tier') == args.context_tier, f"bridge started.context_tier is not {args.context_tier}")
        _require(started.get('backend_available') == 'cuda', 'bridge started.backend_available is not cuda')
        _require(started.get('backend_used') == 'cuda', 'bridge started.backend_used is not cuda')
        _require(
            _offloaded_layer_count(started.get('offloaded_layers')) > 0,
            'bridge started.offloaded_layers must be > 0',
        )
        bridge_kv_cache = str(started.get('kv_cache_device') or '').lower()
        _require(
            bridge_kv_cache not in {'', 'cpu'},
            'bridge started.kv_cache_device indicates CPU-only execution',
        )
        return 0
    except Exception as exc:
        return _fail(str(exc))


if __name__ == '__main__':
    raise SystemExit(main())
