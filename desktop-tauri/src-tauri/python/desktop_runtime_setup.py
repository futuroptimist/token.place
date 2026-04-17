"""Desktop runtime bootstrap for llama-cpp backend availability."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from desktop_gpu_packaging import (
    LlamaCppInstallPlan,
    llama_cpp_install_plan_fallbacks,
    llama_cpp_requirement_spec,
)


@dataclass(frozen=True)
class RuntimeProbe:
    backend: str
    gpu_offload_supported: bool
    detected_device: str
    interpreter: str
    prefix: str
    llama_module_path: str
    error: Optional[str] = None


GPU_MODES = frozenset({"auto", "gpu", "hybrid"})
PIP_INSTALL_TIMEOUT_SECONDS = 300
PIP_SOURCE_BUILD_TIMEOUT_SECONDS = 1800
REEXEC_GUARD_ENV = "TOKEN_PLACE_DESKTOP_RUNTIME_REEXECED"
DISABLE_BOOTSTRAP_ENV = "TOKEN_PLACE_DESKTOP_DISABLE_RUNTIME_BOOTSTRAP"
SOURCE_REPAIR_COOLDOWN_SECONDS = 24 * 60 * 60

_PROBE_SNIPPET = """
import json
import sys
from pathlib import Path

repo_root = Path.cwd()
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from utils.llm.model_manager import detect_llama_runtime_capabilities
# NOTE: detect_llama_runtime_capabilities must keep `import llama_cpp` lazy
# (inside the function body). We sanitize sys.path below before that import
# runs so site-packages can win over a repo-local llama_cpp.py shim.

repo_root_resolved = str(repo_root.resolve())
sanitized = []
for entry in sys.path:
    resolved_entry = str(Path(entry or ".").resolve())
    if resolved_entry == repo_root_resolved:
        continue
    sanitized.append(entry)
sys.path[:] = sanitized

payload = detect_llama_runtime_capabilities()
print(json.dumps(payload))
""".strip()


def _probe_llama_runtime() -> RuntimeProbe:
    repo_root = Path(__file__).resolve().parents[3]
    cmd = [sys.executable, "-c", _PROBE_SNIPPET]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    pythonpath_entries = [str(repo_root)]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_root),
            env=env,
        )
    except Exception as exc:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path="missing",
            error=str(exc),
        )

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode != 0 or not stdout:
        return RuntimeProbe(
            backend="missing",
            gpu_offload_supported=False,
            detected_device="none",
            interpreter=sys.executable,
            prefix=sys.prefix,
            llama_module_path="missing",
            error=stderr or f"probe subprocess failed with return code {result.returncode}",
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = {
            "backend": "missing",
            "gpu_offload_supported": False,
            "detected_device": "none",
            "interpreter": sys.executable,
            "prefix": sys.prefix,
            "llama_module_path": "missing",
            "error": stderr or "probe parse failure",
        }

    return RuntimeProbe(
        backend=str(payload.get("backend", "cpu")),
        gpu_offload_supported=bool(payload.get("gpu_offload_supported", False)),
        detected_device=str(payload.get("detected_device", "cpu")),
        interpreter=str(payload.get("interpreter", sys.executable)),
        prefix=str(payload.get("prefix", sys.prefix)),
        llama_module_path=str(payload.get("llama_module_path", "missing")),
        error=payload.get("error"),
    )


def _run_pip_install(
    cmd: list[str],
    env: dict[str, str],
    *,
    timeout_seconds: int = PIP_INSTALL_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    try:
        install = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"pip install timed out after {timeout_seconds}s"

    if install.returncode == 0:
        return True, (install.stdout or "").strip()

    return False, (install.stderr or install.stdout or "").strip()


def _windows_cuda_source_repair(requirements_path: Path) -> tuple[bool, str]:
    env = os.environ.copy()
    env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
    env["FORCE_CMAKE"] = "1"
    package_spec = "llama-cpp-python"
    try:
        package_spec = llama_cpp_requirement_spec(requirements_path)
    except (FileNotFoundError, OSError, ValueError):
        # Packaged desktop layouts may not ship a repo-root requirements.txt.
        # Degrade gracefully to an unpinned source reinstall in that case.
        package_spec = "llama-cpp-python"
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        package_spec,
        "--force-reinstall",
        "--no-cache-dir",
        "--verbose",
    ]
    return _run_pip_install(cmd, env, timeout_seconds=PIP_SOURCE_BUILD_TIMEOUT_SECONDS)


def _summarize_install_error(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return "install failed"
    return text.splitlines()[-1][:240]


def _probe_result_payload(probe: RuntimeProbe) -> Dict[str, str]:
    return {
        "detected_device": probe.detected_device or "cpu",
        "interpreter": probe.interpreter,
        "prefix": probe.prefix,
        "interpreter_prefix": probe.prefix,
        "llama_module_path": probe.llama_module_path,
    }


def _repo_llama_shim_path(repo_root: Path) -> str:
    return str((repo_root / "llama_cpp.py").resolve())


def _is_repo_local_llama_module(module_path: str, repo_root: Path) -> bool:
    module = str(module_path or "").strip()
    if not module:
        return False
    try:
        resolved_module = os.path.normcase(str(Path(module).resolve())).casefold()
        repo_shim = os.path.normcase(_repo_llama_shim_path(repo_root)).casefold()
        return resolved_module == repo_shim
    except OSError:
        return False


def _runtime_state_path() -> Path:
    return Path.home() / ".token_place_desktop_runtime_state.json"


def _load_runtime_state() -> dict:
    path = _runtime_state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_runtime_state(state: dict) -> None:
    path = _runtime_state_path()
    path.write_text(json.dumps(state), encoding="utf-8")


def _should_attempt_source_repair() -> tuple[bool, str]:
    state = _load_runtime_state()
    failures = state.get("source_repair_failures", {})
    entry = failures.get(sys.executable, {})
    last_failed_at = float(entry.get("last_failed_at", 0))
    now = time.time()
    if now - last_failed_at >= SOURCE_REPAIR_COOLDOWN_SECONDS:
        return True, ""
    retry_in_seconds = int(SOURCE_REPAIR_COOLDOWN_SECONDS - (now - last_failed_at))
    return False, entry.get("reason") or f"source repair cooldown active ({retry_in_seconds}s remaining)"


def _record_source_repair_failure(reason: str) -> None:
    state = _load_runtime_state()
    failures = state.setdefault("source_repair_failures", {})
    failures[sys.executable] = {
        "last_failed_at": time.time(),
        "reason": reason,
    }
    _save_runtime_state(state)


def _clear_source_repair_failure() -> None:
    state = _load_runtime_state()
    failures = state.get("source_repair_failures", {})
    if sys.executable in failures:
        failures.pop(sys.executable, None)
        _save_runtime_state(state)


def maybe_reexec_for_runtime_refresh(
    runtime_setup: Dict[str, str], *, allow_reexec: bool = True
) -> None:
    if not allow_reexec:
        return
    if runtime_setup.get("runtime_action") != "installed_cuda_reexec":
        return
    if os.environ.get(REEXEC_GUARD_ENV) == "1":
        return
    env = os.environ.copy()
    env[REEXEC_GUARD_ENV] = "1"
    try:
        os.execve(sys.executable, [sys.executable, *sys.argv], env)
    except OSError:
        return


def ensure_desktop_llama_runtime(mode: str, *, repo_root: Optional[Path] = None) -> Dict[str, str]:
    """Ensure the sidecar interpreter has a GPU-capable runtime when mode prefers GPU."""

    selected_mode = (mode or "auto").strip().lower()
    target_root = (repo_root or Path(__file__).resolve().parents[3]).resolve()
    before = _probe_llama_runtime()
    if _is_repo_local_llama_module(before.llama_module_path, target_root):
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                "llama_cpp import shadowed by repo-local shim "
                f"({before.llama_module_path}); remove repo root from import precedence "
                "or run via desktop sidecar bootstrap so site-packages llama-cpp-python is used"
            ),
            "runtime_action": "shadowed_repo_llama_cpp",
            **_probe_result_payload(before),
        }

    if selected_mode not in GPU_MODES:
        return {
            "selected_backend": "cpu",
            "fallback_reason": "cpu mode explicitly selected",
            "runtime_action": "skipped",
            **_probe_result_payload(before),
        }

    if before.gpu_offload_supported and before.backend in {"cuda", "metal"}:
        return {
            "selected_backend": before.backend,
            "fallback_reason": "",
            "runtime_action": "already_supported",
            **_probe_result_payload(before),
        }

    if not sys.platform.startswith("win"):
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                f"GPU runtime unavailable ({before.error or before.backend}); "
                "desktop auto-repair is currently Windows-focused"
            ),
            "runtime_action": "probe_only",
            **_probe_result_payload(before),
        }

    if os.getenv(DISABLE_BOOTSTRAP_ENV) == "1":
        return {
            "selected_backend": "cpu",
            "fallback_reason": (
                f"desktop runtime bootstrap disabled by {DISABLE_BOOTSTRAP_ENV}=1"
            ),
            "runtime_action": "probe_only",
            **_probe_result_payload(before),
        }

    requirements_path = target_root / "requirements.txt"
    last_error = ""

    should_repair, repair_skip_reason = _should_attempt_source_repair()
    if should_repair:
        source_ok, source_log = _windows_cuda_source_repair(requirements_path)
        if source_ok:
            _clear_source_repair_failure()
            after = _probe_llama_runtime()
            if after.gpu_offload_supported and after.backend == "cuda":
                return {
                    "selected_backend": "cuda",
                    "fallback_reason": "installed CUDA runtime; re-executing sidecar",
                    "runtime_action": "installed_cuda_reexec",
                    **_probe_result_payload(after),
                }
            last_error = (
                "CUDA source reinstall completed but runtime still CPU-only; "
                "check CUDA toolkit/build tools"
            )
            _record_source_repair_failure(last_error)
        else:
            last_error = _summarize_install_error(source_log)
            _record_source_repair_failure(last_error)
    else:
        last_error = repair_skip_reason

    try:
        plans = llama_cpp_install_plan_fallbacks(
            platform=sys.platform,
            requirements_path=requirements_path,
        )
    except (FileNotFoundError, ValueError):
        plans = _fallback_unpinned_plans(sys.platform)

    for plan in plans:
        env = os.environ.copy()
        env.update(plan.pip_env())
        cmd = [sys.executable, "-m", "pip", "install", *plan.pip_install_args(), plan.package_spec]
        ok, log_output = _run_pip_install(cmd, env)
        if not ok:
            last_error = _summarize_install_error(log_output)
            continue

        after = _probe_llama_runtime()
        if after.gpu_offload_supported and after.backend in {"cuda", "metal"}:
            return {
                "selected_backend": after.backend,
                "fallback_reason": "installed GPU runtime; re-executing sidecar",
                "runtime_action": "installed_cuda_reexec",
                **_probe_result_payload(after),
            }

        if plan.backend == "cpu":
            return {
                "selected_backend": "cpu",
                "fallback_reason": "GPU runtime unavailable after repair; using CPU runtime",
                "runtime_action": "installed_cpu_fallback",
                **_probe_result_payload(after),
            }

    return {
        "selected_backend": "cpu",
        "fallback_reason": before.error or last_error or "unable to install a GPU-capable runtime",
        "runtime_action": "failed",
        **_probe_result_payload(before),
    }


def _fallback_unpinned_plans(platform: str) -> list[LlamaCppInstallPlan]:
    detected_platform = platform.lower()
    if detected_platform.startswith("win"):
        return [
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="cuda",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124",
                extra_index_url="https://pypi.org/simple",
                only_binary=True,
                no_binary=False,
            ),
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="cpu",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url="https://pypi.org/simple",
                extra_index_url=None,
                only_binary=True,
                no_binary=False,
            ),
        ]

    if detected_platform == "darwin":
        return [
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="metal",
                package_spec="llama-cpp-python",
                cmake_args=None,
                force_cmake=False,
                index_url="https://abetlen.github.io/llama-cpp-python/whl/metal",
                extra_index_url="https://pypi.org/simple",
                only_binary=True,
                no_binary=False,
            ),
            LlamaCppInstallPlan(
                platform=detected_platform,
                backend="metal",
                package_spec="llama-cpp-python",
                cmake_args="-DGGML_METAL=on -DGGML_NATIVE=off",
                force_cmake=True,
                index_url="https://pypi.org/simple",
                extra_index_url=None,
                only_binary=False,
                no_binary=True,
            ),
        ]

    return [
        LlamaCppInstallPlan(
            platform=detected_platform,
            backend="cpu",
            package_spec="llama-cpp-python",
            cmake_args=None,
            force_cmake=False,
            index_url=None,
            extra_index_url=None,
            only_binary=False,
            no_binary=False,
        )
    ]
