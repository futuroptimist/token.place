#!/usr/bin/env python3
"""Install llama-cpp-python for desktop with platform GPU preference + safe fallback."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys


def _is_bad_zip_error(output: str) -> bool:
    lower = output.lower()
    return "bad crc-32" in lower or "badzipfile" in lower


def main() -> int:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    desktop_python_root = repo_root / "desktop-tauri" / "src-tauri" / "python"
    if str(desktop_python_root) not in sys.path:
        sys.path.insert(0, str(desktop_python_root))

    from desktop_gpu_packaging import llama_cpp_install_plan_fallbacks

    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=True)

    plans = llama_cpp_install_plan_fallbacks(
        platform=sys.platform,
        requirements_path=repo_root / "requirements.txt",
    )

    for plan_index, plan in enumerate(plans, start=1):
        install_args = plan.pip_install_args()
        command = [sys.executable, "-m", "pip", "install", *install_args, plan.package_spec]
        env = os.environ.copy()
        env.update(plan.pip_env())

        print(
            f"Installing {plan.package_spec} for backend={plan.backend} on platform={sys.platform}"
        )
        print(f"Install plan {plan_index}/{len(plans)}")
        if plan.index_url:
            print(f"Using primary wheel index: {plan.index_url}")
        if plan.extra_index_url:
            print(f"Using extra package index: {plan.extra_index_url}")
        if plan.pip_env():
            print(f"Using build env: {plan.pip_env()}")

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            result = subprocess.run(
                command,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return 0

            combined_output = f"{result.stdout}\n{result.stderr}"
            print(combined_output)

            if _is_bad_zip_error(combined_output):
                print("Detected corrupted wheel archive; switching to next fallback plan.")
                break

            if attempt >= max_attempts:
                print(f"Install attempt {attempt} failed for plan {plan_index}.")
            else:
                print(f"Install attempt {attempt} failed, retrying...")

        print(f"Falling back from plan {plan_index} to next candidate...")
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "llama-cpp-python"],
            check=False,
        )

    raise RuntimeError("Failed to install llama-cpp-python for all configured plans")


if __name__ == "__main__":
    raise SystemExit(main())
