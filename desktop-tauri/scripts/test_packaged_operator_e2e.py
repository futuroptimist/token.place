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


REPO_ROOT = Path(__file__).resolve().parents[2]


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_livez(relay: subprocess.Popen[str], port: int, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/livez", timeout=1) as resp:  # noqa: S310
                if resp.status == 200:
                    return
        except Exception as exc:  # pragma: no cover - retry loop
            last_error = exc

        if relay.poll() is not None:
            stderr = relay.stderr.read() if relay.stderr else ""
            stdout = relay.stdout.read() if relay.stdout else ""
            raise RuntimeError(
                f"relay exited early with code {relay.returncode}; stdout={stdout}; stderr={stderr}"
            )
        time.sleep(0.25)

    raise RuntimeError(f"relay did not become live on port {port}: {last_error}")


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


def _packaged_env(tmp_root: Path, resources_root: Path | None = None) -> dict[str, str]:
    resources_root = resources_root or (tmp_root / "resources")
    home_dir = tmp_root / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PYTHONNOUSERSITE"] = "1"
    env["TOKEN_PLACE_PYTHON_IMPORT_ROOT"] = str(resources_root)
    env["PYTHONPATH"] = str(resources_root / "python")
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

def enqueue_bridge_stdout(stdout: object, output_queue: queue.Queue[bytes]) -> None:
    if not hasattr(stdout, "readline"):
        return

    readline = stdout.readline
    while True:
        chunk = readline()
        if not chunk:
            return
        output_queue.put(chunk)


def main() -> int:
    relay_port = reserve_free_port()
    env = os.environ.copy()
    env["USE_MOCK_LLM"] = "1"

    with tempfile.TemporaryDirectory(prefix="token-place-packaged-e2e-") as tmpdir:
        tmp_path = Path(tmpdir)
        bridge_script = create_packaged_layout(tmp_path)
        run_desktop_dependency_preflight(tmp_path)
        run_model_bridge_inspect_probe(tmp_path)
        run_compute_bridge_import_probe(tmp_path)

        create_macos_bundle_layout(tmp_path)
        mac_resources_root = tmp_path / "TokenPlace.app" / "Contents" / "Resources"
        run_desktop_dependency_preflight(tmp_path, resources_root=mac_resources_root)
        run_model_bridge_inspect_probe(tmp_path, resources_root=mac_resources_root)
        run_compute_bridge_import_probe(tmp_path, resources_root=mac_resources_root)

        if os.environ.get("TOKEN_PLACE_INSPECT_ONLY") == "1":
            return 0

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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        bridge: subprocess.Popen[bytes] | None = None
        bridge_output = ""
        output_queue: queue.Queue[bytes] | None = None
        try:
            wait_for_livez(relay, relay_port)
            bridge = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    str(bridge_script),
                    "--model",
                    "mock.gguf",
                    "--mode",
                    "cpu",
                    "--relay-url",
                    f"http://127.0.0.1:{relay_port}",
                ],
                cwd=tmpdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )

            assert bridge.stdout is not None
            assert bridge.stdin is not None

            output_queue = queue.Queue()
            threading.Thread(
                target=enqueue_bridge_stdout,
                args=(bridge.stdout, output_queue),
                daemon=True,
            ).start()

            saw_started = False
            saw_registered = False
            buffered = ""
            deadline = time.time() + 20

            while time.time() < deadline:
                timeout = max(0.0, min(0.25, deadline - time.time()))
                try:
                    chunk = output_queue.get(timeout=timeout)
                except queue.Empty:
                    if bridge.poll() is not None:
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
                    if payload.get("registered") is True:
                        saw_registered = True
                    if saw_started and saw_registered:
                        bridge.stdin.write(b'{"type":"cancel"}\n')
                        bridge.stdin.flush()
                        break

                if saw_started and saw_registered:
                    break

            if not saw_started:
                raise RuntimeError(
                    "bridge did not emit started/running event; output="
                    f"{bridge_output[-2000:]}"
                )
            if not saw_registered:
                raise RuntimeError(
                    "bridge never reported registered=true (relay connection missing); output="
                    f"{bridge_output[-2000:]}"
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
                raise RuntimeError(f"bridge exited non-zero ({bridge.returncode}): {bridge_output}")
            forbidden_output = (
                "No module named",
                "ModuleNotFoundError",
                "ImportError",
                "compute-node bridge exited before emitting a startup event",
                "desktop_runtime_setup module missing",
            )
            for marker in forbidden_output:
                assert marker not in bridge_output, bridge_output

        finally:
            if bridge is not None and bridge.poll() is None:
                bridge.kill()
            if relay.poll() is None:
                relay.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
