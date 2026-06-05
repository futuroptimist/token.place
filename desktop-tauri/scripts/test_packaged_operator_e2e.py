#!/usr/bin/env python3
"""End-to-end regression for packaged operator Python bridge imports."""

from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen


RELAY_STARTUP_TIMEOUT_SECONDS = 60.0
RELAY_LIVEZ_REQUEST_TIMEOUT_SECONDS = 2.0


REPO_ROOT = Path(__file__).resolve().parents[2]


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _tail_text(path: Path, *, max_chars: int = 4000) -> str:
    try:
        return path.read_text(errors="replace")[-max_chars:]
    except OSError as exc:
        return f"<unable to read {path}: {exc}>"


def wait_for_livez(
    relay: subprocess.Popen[str],
    port: int,
    *,
    timeout_seconds: float = RELAY_STARTUP_TIMEOUT_SECONDS,
    request_timeout_seconds: float = RELAY_LIVEZ_REQUEST_TIMEOUT_SECONDS,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/livez",
                timeout=request_timeout_seconds,
            ) as resp:  # noqa: S310
                if resp.status == 200:
                    return
        except Exception as exc:  # pragma: no cover - retry loop
            last_error = exc

        if relay.poll() is not None:
            stderr = _tail_text(stderr_path) if stderr_path is not None else ""
            stdout = _tail_text(stdout_path) if stdout_path is not None else ""
            raise RuntimeError(
                f"relay exited early with code {relay.returncode}; stdout={stdout}; stderr={stderr}"
            )
        time.sleep(0.25)

    stdout = _tail_text(stdout_path) if stdout_path is not None else ""
    stderr = _tail_text(stderr_path) if stderr_path is not None else ""
    raise RuntimeError(
        f"relay did not become live on port {port} after {timeout_seconds:.1f}s: "
        f"{last_error}; stdout={stdout}; stderr={stderr}"
    )


def create_packaged_layout(tmp_root: Path, *, resources_dir_name: str = "resources") -> Path:
    resources_root = tmp_root / resources_dir_name
    python_dir = resources_root / "python"
    python_dir.mkdir(parents=True, exist_ok=True)

    for filename in (
        "compute_node_bridge.py",
        "desktop_runtime_setup.py",
        "desktop_gpu_packaging.py",
        "inference_sidecar.py",
        "model_bridge.py",
        "path_bootstrap.py",
        "requirements_desktop_runtime.txt",
    ):
        shutil.copy2(
            REPO_ROOT / "desktop-tauri" / "src-tauri" / "python" / filename,
            python_dir / filename,
        )

    shutil.copy2(REPO_ROOT / "config.py", resources_root / "config.py")
    shutil.copy2(REPO_ROOT / "encrypt.py", resources_root / "encrypt.py")
    shutil.copy2(REPO_ROOT / "requirements.txt", resources_root / "requirements.txt")
    shutil.copytree(REPO_ROOT / "utils", resources_root / "utils", dirs_exist_ok=True)

    return python_dir / "compute_node_bridge.py"


def create_macos_bundle_layout(tmp_root: Path) -> Path:
    resources_tmp_root = tmp_root / "TokenPlace.app" / "Contents"
    return create_packaged_layout(resources_tmp_root, resources_dir_name="Resources")


def _packaged_env(
    tmp_root: Path,
    resources_root: Path | None = None,
    *,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    resources_root = resources_root or (tmp_root / "resources")
    home_dir = tmp_root / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env["TOKEN_PLACE_PYTHON_IMPORT_ROOT"] = str(resources_root)
    env["PYTHONPATH"] = str(resources_root / "python")
    if extra_env:
        env.update(extra_env)
    return env


def run_desktop_dependency_preflight(tmp_root: Path, *, resources_root: Path | None = None) -> None:
    resources_root = resources_root or (tmp_root / "resources")
    env = _packaged_env(tmp_root, resources_root)

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys; "
                "sys.path.insert(0, r'" + str(resources_root / 'python') + "'); "
                "import desktop_runtime_setup as mod; "
                "print(json.dumps(mod.ensure_desktop_python_dependencies()))"
            ),
        ],
        cwd=tmp_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    payload = json.loads(result.stdout.strip())
    assert payload.get("ok") == "true", combined


def run_model_bridge_inspect_probe(tmp_root: Path, *, resources_root: Path | None = None) -> None:
    resources_root = resources_root or (tmp_root / "resources")
    env = _packaged_env(tmp_root, resources_root)

    model_bridge = resources_root / "python" / "model_bridge.py"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(model_bridge), "inspect"],
        cwd=tmp_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    parsed = json.loads(result.stdout.strip())
    assert parsed.get("ok") is True, combined
    payload = parsed.get("payload")
    assert isinstance(payload, dict), combined
    required_keys = {
        "canonical_family_url",
        "filename",
        "url",
        "models_dir",
        "resolved_model_path",
        "exists",
        "size_bytes",
    }
    assert required_keys.issubset(payload.keys()), combined

    forbidden_any_output = [
        "Model bridge failure",
        "unsupported operand type(s) for |",
        "Missing Python dependency for model downloads",
        "No module named",
        "ModuleNotFoundError",
        "ImportError",
        "NotOpenSSLWarning",
        "~/Library/Python",
    ]
    for marker in forbidden_any_output:
        assert marker not in combined, combined

    # model_bridge inspect JSON payload can legitimately include absolute model paths
    # in stdout, so user-home leakage checks are constrained to stderr diagnostics.
    forbidden_stderr_only = ["/Users/", "~/Library/Python"]
    for marker in forbidden_stderr_only:
        assert marker not in result.stderr, combined

    check_imports = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import pathlib,sys; "
                "python_dir=pathlib.Path(r'" + str(model_bridge.parent) + "'); "
                "sys.path.insert(0,str(python_dir)); "
                "import desktop_runtime_setup as mod; "
                "payload=mod.ensure_desktop_python_dependencies(); "
                "assert payload.get('ok')=='true', payload; "
                "import psutil,requests,dotenv,cryptography; "
                "print('desktop-runtime-imports-ok')"
            ),
        ],
        cwd=tmp_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check_imports.returncode == 0, f"{check_imports.stdout}\n{check_imports.stderr}"
    assert "desktop-runtime-imports-ok" in check_imports.stdout


def run_compute_bridge_import_probe(tmp_root: Path, *, resources_root: Path | None = None) -> None:
    resources_root = resources_root or (tmp_root / "resources")
    env = _packaged_env(tmp_root, resources_root)

    compute_bridge = resources_root / "python" / "compute_node_bridge.py"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, pathlib; "
                "path = pathlib.Path(r'" + str(compute_bridge) + "'); "
                "spec = importlib.util.spec_from_file_location('compute_node_bridge_probe', path); "
                "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)"
            ),
        ],
        cwd=tmp_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined

    forbidden_any_output = [
        "No module named 'requests'",
        "ModuleNotFoundError",
        "ImportError",
    ]
    for marker in forbidden_any_output:
        assert marker not in combined, combined


def run_unified_root_import_policy_probe(
    tmp_root: Path, *, resources_root: Path | None = None
) -> None:
    resources_root = resources_root or (tmp_root / "resources")
    env = _packaged_env(tmp_root, resources_root)
    userbase = tmp_root / "userbase"
    fake_user_site = (
        userbase
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    fake_user_site.mkdir(parents=True, exist_ok=True)
    repo_like_cwd = tmp_root / "repo-like-cwd"
    repo_like_cwd.mkdir(parents=True, exist_ok=True)
    (repo_like_cwd / "llama_cpp.py").write_text(
        "raise RuntimeError('repo shim imported')\n", encoding="utf-8"
    )
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(resources_root / "python"),
            str(resources_root),
            str(fake_user_site),
            str(repo_like_cwd),
        ]
    )
    env["PYTHONUSERBASE"] = str(userbase)

    bootstrap = resources_root / "python" / "path_bootstrap.py"
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import json, pathlib, site, sys; "
                f"sys.path.insert(0, {str(bootstrap.parent)!r}); "
                "from path_bootstrap import ensure_runtime_import_paths; "
                f"ensure_runtime_import_paths({str(bootstrap)!r}); "
                "payload={"
                "'import_root': str(pathlib.Path(__import__('os').environ['TOKEN_PLACE_PYTHON_IMPORT_ROOT']).resolve()), "
                "'first_path': str(pathlib.Path(sys.path[0]).resolve()), "
                "'has_utils': pathlib.Path(sys.path[0], 'utils').is_dir(), "
                "'has_config': pathlib.Path(sys.path[0], 'config.py').is_file(), "
                "'user_site_present': any(pathlib.Path(p or '.').resolve() == pathlib.Path(site.USER_SITE).resolve() for p in sys.path if site.USER_SITE), "
                "'cwd_present': any(pathlib.Path(p or '.').resolve() == pathlib.Path.cwd().resolve() for p in sys.path), "
                "'llama_shim_before_site': next((i for i,p in enumerate(sys.path) if pathlib.Path(p or '.', 'llama_cpp.py').is_file()), 9999) < next((i for i,p in enumerate(sys.path) if 'site-packages' in p or 'dist-packages' in p), 9999)"
                "}; print(json.dumps(payload, sort_keys=True))"
            ),
        ],
        cwd=repo_like_cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    payload = json.loads(result.stdout.strip())
    assert payload["first_path"] == str(resources_root.resolve()), combined
    assert payload["has_utils"] is True, combined
    assert payload["has_config"] is True, combined
    assert payload["user_site_present"] is False, combined
    assert payload["cwd_present"] is False, combined
    assert payload["llama_shim_before_site"] is False, combined


def create_fake_llama_cpp_site(tmp_root: Path, layout_label: str) -> tuple[Path, Path]:
    fake_site = tmp_root / f"fake site-packages {layout_label.replace('/', '_')}"
    fake_pkg = fake_site / "llama_cpp"
    fake_pkg.mkdir(parents=True, exist_ok=True)
    fake_init = fake_pkg / "__init__.py"
    fake_init.write_text(
        "import os, time\n"
        "if os.environ.get('TOKEN_PLACE_LLAMA_CPP_PROBE_SYS_PATH'):\n"
        "    time.sleep(60)\n"
        "__file__ = __file__\n"
        "GGML_USE_CUDA = True\n"
        "def llama_supports_gpu_offload():\n"
        "    return True\n"
        "class Llama:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        self.args = args\n"
        "        self.kwargs = kwargs\n"
        "    def create_chat_completion(self, *args, **kwargs):\n"
        "        return {'choices': [{'message': {'role': 'assistant', 'content': 'fake llama ok'}}]}\n",
        encoding="utf-8",
    )
    return fake_site, fake_init

def run_llama_cpp_watchdog_regression_probe(
    tmp_root: Path, *, resources_root: Path | None = None, layout_label: str = "standard resources"
) -> None:
    """Assert packaged model warm-load does not pre-import llama_cpp in a divergent child."""

    resources_root = resources_root or (tmp_root / "resources")
    fake_site, fake_init = create_fake_llama_cpp_site(tmp_root, layout_label)
    env = _packaged_env(
        tmp_root,
        resources_root,
        extra_env={
            "TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS": "1",
            "PYTHONPATH": os.pathsep.join(
                [str(fake_site), str(resources_root / "python"), str(resources_root)]
            ),
        },
    )
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys; "
                f"sys.path.insert(0, {str(fake_site)!r}); "
                f"sys.path.insert(0, {str(resources_root)!r}); "
                "from utils.llm import model_manager; "
                "module_path = pathlib.Path(sys.path[1], 'llama_cpp', '__init__.py'); "
                "llama_cpp = model_manager._import_llama_cpp_runtime("
                "require_real_runtime=True, "
                "desktop_runtime_probe={"
                "'selected_backend': 'cuda', 'gpu_offload_supported': True, "
                "'detected_device': 'cuda', 'interpreter': sys.executable, "
                "'prefix': sys.prefix, 'llama_module_path': str(module_path), "
                "'fallback_reason': ''}); "
                "print(json.dumps({'module_path': getattr(llama_cpp, '__file__', None)}))"
            ),
        ],
        cwd=tmp_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(payload["module_path"]).resolve() == fake_init.resolve(), combined
    assert "llama_cpp_import_timeout" not in combined, combined
    assert "llama_cpp import watchdog start" not in combined, combined


def run_llama_cpp_facade_early_exit_regression_probe(
    tmp_root: Path, *, resources_root: Path | None = None, layout_label: str = "standard resources"
) -> None:
    """Assert facade child early exits surface diagnostics and cannot look registered."""

    resources_root = resources_root or (tmp_root / "resources")
    fake_site = tmp_root / f"fake exiting site-packages {layout_label.replace('/', '_')}"
    fake_pkg = fake_site / "llama_cpp"
    fake_pkg.mkdir(parents=True, exist_ok=True)
    (fake_pkg / "__init__.py").write_text(
        "import sys\n"
        "print('fake facade import stdout before exit')\n"
        "print('fake facade import stderr before exit', file=sys.stderr)\n"
        "raise SystemExit(19)\n",
        encoding="utf-8",
    )
    env = _packaged_env(
        tmp_root,
        resources_root,
        extra_env={
            "TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS": "2",
            "PYTHONPATH": os.pathsep.join(
                [str(fake_site), str(resources_root / "python"), str(resources_root)]
            ),
        },
    )
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "\n".join(
                    [
                        "import pathlib, sys",
                        f"sys.path.insert(0, {str(fake_site)!r})",
                        f"sys.path.insert(0, {str(resources_root)!r})",
                        "from utils.llm import model_manager",
                        "module_path = pathlib.Path(sys.path[1], 'llama_cpp', '__init__.py')",
                        "facade = model_manager._SubprocessLlamaCppModule(str(module_path), timeout_seconds=2, desktop_runtime_probe={"
                        "    'selected_backend': 'cuda', 'gpu_offload_supported': True, "
                        "    'detected_device': 'cuda', 'interpreter': sys.executable, "
                        "    'prefix': sys.prefix, 'llama_module_path': str(module_path)})",
                        "try:",
                        "    facade.Llama(model_path='fake.gguf')",
                        "except RuntimeError as exc:",
                        "    print(str(exc))",
                        "    raise SystemExit(0)",
                        "raise SystemExit('facade early-exit probe unexpectedly succeeded')",
                    ]
                )
            ),
        ],
        cwd=tmp_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "llama_cpp_import subprocess exited before JSON handshake" in combined, combined
    assert "exit_code=19" in combined, combined
    assert "fake facade import stdout before exit" in combined, combined
    assert "fake facade import stderr before exit" in combined, combined
    assert "registered=true" not in combined.lower(), combined
    assert "subprocess ended" not in combined, combined


def enqueue_bridge_stdout(stdout: object, output_queue: queue.Queue[bytes]) -> None:
    if not hasattr(stdout, "readline"):
        return

    readline = stdout.readline
    while True:
        chunk = readline()
        if not chunk:
            return
        output_queue.put(chunk)


def run_compute_bridge_startup_probe(
    tmp_root: Path,
    bridge_script: Path,
    *,
    relay_port: int,
    resources_root: Path | None = None,
    layout_label: str = "standard resources",
    use_mock_llm: str = "1",
    mode: str = "cpu",
    model_path: Path | str | None = None,
    extra_env: dict[str, str] | None = None,
) -> str:
    packaged_extra_env = {"USE_MOCK_LLM": use_mock_llm}
    if extra_env:
        packaged_extra_env.update(extra_env)
    env = _packaged_env(tmp_root, resources_root, extra_env=packaged_extra_env)
    model_arg = str(model_path or "mock.gguf")
    log_dir = REPO_ROOT / ".desktop-e2e-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_layout_label = layout_label.replace(" ", "_").replace("/", "_")
    log_file = log_dir / f"packaged-bridge-startup-{safe_layout_label}.log"
    bridge = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            str(bridge_script),
            "--model",
            model_arg,
            "--mode",
            mode,
            "--relay-url",
            f"http://127.0.0.1:{relay_port}",
        ],
        cwd=tmp_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
    )
    bridge_output = ""
    saw_started = False
    saw_ready_registered = False
    last_running_unregistered_payload: dict[str, object] | None = None
    try:
        assert bridge.stdout is not None
        assert bridge.stdin is not None
        output_queue: queue.Queue[bytes] = queue.Queue()
        threading.Thread(
            target=enqueue_bridge_stdout,
            args=(bridge.stdout, output_queue),
            daemon=True,
        ).start()

        start_deadline = time.time() + 20
        registration_deadline = time.time() + 90
        buffered = ""
        while time.time() < registration_deadline:
            active_deadline = start_deadline if not saw_started else registration_deadline
            timeout = max(0.0, min(0.25, active_deadline - time.time()))
            if timeout <= 0 and not saw_started:
                break
            try:
                chunk = output_queue.get(timeout=timeout)
            except queue.Empty:
                if bridge.poll() is not None:
                    break
                if saw_started and time.time() >= registration_deadline:
                    break
                continue

            text = chunk.decode("utf-8", errors="replace")
            bridge_output += text
            buffered += text

            while "\n" in buffered:
                line, buffered = buffered.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "error":
                    raise RuntimeError(f"bridge emitted error event: {payload}")
                if payload.get("type") == "started" and payload.get("running") is True:
                    saw_started = True
                if payload.get("running") is True and payload.get("registered") is not True:
                    last_running_unregistered_payload = payload
                if (
                    payload.get("registered") is True
                    and payload.get("relay_runtime_state") == "ready"
                ):
                    saw_ready_registered = True
                if saw_started and saw_ready_registered:
                    bridge.stdin.write(b'{"type":"cancel"}\n')
                    bridge.stdin.flush()
                    cancel_deadline = time.time() + 5
                    while time.time() < cancel_deadline and bridge.poll() is None:
                        try:
                            bridge_output += output_queue.get(timeout=0.1).decode("utf-8", errors="replace")
                        except queue.Empty:
                            pass
                    break
            if saw_started and saw_ready_registered:
                break

        if not saw_started:
            raise RuntimeError(
                f"[{layout_label}] bridge did not emit started/running event; output="
                f"{bridge_output[-4000:]}"
            )
        if not saw_ready_registered:
            raise RuntimeError(
                f"[{layout_label}] bridge never reported registered=true with "
                f"relay_runtime_state=ready (relay connection/runtime missing); "
                f"last_running_unregistered_payload={last_running_unregistered_payload}; "
                f"output={bridge_output[-4000:]}"
            )

        try:
            bridge.stdin.close()
        except OSError:
            pass

        try:
            bridge.wait(timeout=90)
        except subprocess.TimeoutExpired:
            bridge.terminate()
            bridge.wait(timeout=15)
        if bridge.returncode != 0:
            raise RuntimeError(
                f"[{layout_label}] bridge exited non-zero ({bridge.returncode}): "
                f"{bridge_output[-4000:]}"
            )

        forbidden_output = (
            "No module named 'cryptography'",
            "ModuleNotFoundError",
            "ImportError",
            "compute-node bridge exited before emitting a startup event",
            "desktop_runtime_setup module missing",
            "llama_cpp_import_timeout",
            "llama_cpp import watchdog start",
            "llama_cpp_import subprocess ended",
            "Running: yes / Registered: no",
        )
        for marker in forbidden_output:
            assert marker not in bridge_output, bridge_output
        return bridge_output
    finally:
        log_file.write_text(bridge_output, encoding="utf-8")
        if bridge.poll() is None:
            bridge.kill()


def run_llama_cpp_watchdog_packaged_bridge_lifecycle_probe(
    tmp_root: Path,
    bridge_script: Path,
    *,
    relay_port: int,
    resources_root: Path | None = None,
    layout_label: str = "standard resources",
) -> None:
    resources_root = resources_root or (tmp_root / "resources")
    fake_site, fake_init = create_fake_llama_cpp_site(tmp_root, f"lifecycle {layout_label}")
    fake_model = tmp_root / f"fake model {layout_label.replace('/', '_')}.gguf"
    fake_model.write_bytes(b"GGUF fake packaged bridge regression model")
    output = run_compute_bridge_startup_probe(
        tmp_root,
        bridge_script,
        relay_port=relay_port,
        resources_root=resources_root,
        layout_label=f"{layout_label} fake llama_cpp lifecycle",
        use_mock_llm="0",
        mode="auto",
        model_path=fake_model,
        extra_env={
            "TOKEN_PLACE_LLAMA_CPP_RUNTIME_STAGE_TIMEOUT_SECONDS": "1",
            "PYTHONPATH": os.pathsep.join(
                [str(fake_site), str(resources_root / "python"), str(resources_root)]
            ),
        },
    )
    assert str(fake_init) in output, output


def main() -> int:
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"

    with tempfile.TemporaryDirectory(prefix="token-place-packaged-e2e-") as tmpdir:
        tmp_path = Path(tmpdir)
        bridge_script = create_packaged_layout(tmp_path)
        run_desktop_dependency_preflight(tmp_path)
        run_unified_root_import_policy_probe(tmp_path)
        run_model_bridge_inspect_probe(tmp_path)
        run_compute_bridge_import_probe(tmp_path)
        run_llama_cpp_watchdog_regression_probe(tmp_path)
        run_llama_cpp_facade_early_exit_regression_probe(tmp_path)

        mac_bridge_script = create_macos_bundle_layout(tmp_path)
        mac_resources_root = tmp_path / "TokenPlace.app" / "Contents" / "Resources"
        run_desktop_dependency_preflight(tmp_path, resources_root=mac_resources_root)
        run_unified_root_import_policy_probe(tmp_path, resources_root=mac_resources_root)
        run_model_bridge_inspect_probe(tmp_path, resources_root=mac_resources_root)
        run_compute_bridge_import_probe(tmp_path, resources_root=mac_resources_root)
        run_llama_cpp_watchdog_regression_probe(
            tmp_path, resources_root=mac_resources_root, layout_label="macOS Contents/Resources"
        )
        run_llama_cpp_facade_early_exit_regression_probe(
            tmp_path, resources_root=mac_resources_root, layout_label="macOS Contents/Resources"
        )

        if os.environ.get("TOKEN_PLACE_INSPECT_ONLY") == "1":
            return 0

        probe_specs = (
            (bridge_script, None, "standard resources"),
            (mac_bridge_script, mac_resources_root, "macOS Contents/Resources"),
        )

        for probe_script, probe_resources_root, layout_label in probe_specs:
            relay_port = reserve_free_port()
            relay_log_label = layout_label.replace(" ", "-").replace("/", "-")
            relay_stdout = tmp_path / f"relay-{relay_log_label}-{relay_port}.stdout.log"
            relay_stderr = tmp_path / f"relay-{relay_log_label}-{relay_port}.stderr.log"
            relay_stdout_handle = relay_stdout.open("w", encoding="utf-8")
            relay_stderr_handle = relay_stderr.open("w", encoding="utf-8")
            relay = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    str(REPO_ROOT / "relay.py"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(relay_port),
                    "--use_mock_llm",
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=relay_stdout_handle,
                stderr=relay_stderr_handle,
                text=True,
            )

            try:
                wait_for_livez(
                    relay,
                    relay_port,
                    stdout_path=relay_stdout,
                    stderr_path=relay_stderr,
                )
                run_compute_bridge_startup_probe(
                    tmp_path,
                    probe_script,
                    relay_port=relay_port,
                    resources_root=probe_resources_root,
                    layout_label=layout_label,
                )
                run_llama_cpp_watchdog_packaged_bridge_lifecycle_probe(
                    tmp_path,
                    probe_script,
                    relay_port=relay_port,
                    resources_root=probe_resources_root,
                    layout_label=layout_label,
                )
            finally:
                if relay.poll() is None:
                    relay.terminate()
                    try:
                        relay.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        relay.kill()
                        relay.wait(timeout=5)
                relay_stdout_handle.close()
                relay_stderr_handle.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
