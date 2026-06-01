"""Shared helpers for desktop packaged-operator e2e scripts."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[2]
RELAY_STARTUP_TIMEOUT_SECONDS = 60.0
RELAY_LIVEZ_REQUEST_TIMEOUT_SECONDS = 2.0


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
            with urlopen(  # noqa: S310
                f"http://127.0.0.1:{port}/livez",
                timeout=request_timeout_seconds,
            ) as resp:
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
