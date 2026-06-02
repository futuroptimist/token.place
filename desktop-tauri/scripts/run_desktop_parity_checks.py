#!/usr/bin/env python3
"""Single entry point for desktop Windows/macOS parity validation.

The default profile runs the dependency-isolated packaged bridge smoke and the
local API v1 E2EE relay parity harness. Optional flags add platform/hardware
runtime probes without making CI depend on CUDA/Metal or a real GGUF.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent


def _script(name: str) -> Path:
    return SCRIPT_DIR / name


def _run(label: str, command: list[str], *, env: dict[str, str] | None = None, dry_run: bool = False) -> int:
    printable = " ".join(command)
    print(f"\n==> {label}\n{printable}", flush=True)
    if dry_run:
        return 0
    completed = subprocess.run(command, cwd=REPO_ROOT, env=env, check=False)  # noqa: S603
    return int(completed.returncode)


def _append_runtime_checks(args: argparse.Namespace, commands: list[tuple[str, list[str], dict[str, str] | None]]) -> None:
    if not args.model:
        return
    commands.append(
        (
            "desktop runtime resolver/probe",
            [
                sys.executable,
                str(_script("verify_desktop_runtime.py")),
                "--mode",
                args.mode,
                "--model",
                args.model,
            ],
            None,
        )
    )
    if args.gpu_smoke and platform.system() == "Windows":
        commands.append(
            (
                "Windows NVIDIA CUDA desktop smoke",
                [
                    sys.executable,
                    str(_script("windows_nvidia_gpu_smoke_test.py")),
                    "--mode",
                    args.mode,
                    "--model",
                    args.model,
                ],
                None,
            )
        )
    elif args.gpu_smoke:
        print("Skipping Windows NVIDIA GPU smoke because this host is not Windows.", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-packaged",
        action="store_true",
        help="Skip dependency-isolated packaged bridge smoke.",
    )
    parser.add_argument(
        "--skip-relay-parity",
        action="store_true",
        help="Skip local API v1 E2EE relay parity e2e.",
    )
    parser.add_argument(
        "--include-macos-no-relay",
        action="store_true",
        help="On macOS, also require the built-app no-relay lifecycle e2e.",
    )
    parser.add_argument(
        "--model",
        help="Optional GGUF path for runtime probe or hardware smoke checks.",
    )
    parser.add_argument(
        "--mode",
        default="auto",
        choices=("auto", "cpu", "gpu", "hybrid", "metal", "cuda"),
        help="Compute mode for optional runtime checks.",
    )
    parser.add_argument(
        "--gpu-smoke",
        action="store_true",
        help="Add platform GPU smoke checks when supported by the local host.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    commands: list[tuple[str, list[str], dict[str, str] | None]] = []
    if not args.skip_packaged:
        commands.append(
            (
                "packaged operator bridge e2e",
                [sys.executable, str(_script("test_packaged_operator_e2e.py"))],
                None,
            )
        )
    if not args.skip_relay_parity:
        commands.append(
            (
                "local API v1 E2EE relay parity e2e",
                [sys.executable, str(_script("test_desktop_relay_operator_parity_e2e.py"))],
                None,
            )
        )
    if args.include_macos_no_relay:
        if platform.system() == "Darwin":
            env = os.environ.copy()
            env["TOKENPLACE_REQUIRE_NO_RELAY_E2E"] = "1"
            commands.append(
                (
                    "macOS no-relay lifecycle e2e",
                    [sys.executable, str(_script("test_desktop_no_relay_autostart_e2e.py"))],
                    env,
                )
            )
        else:
            print("Skipping macOS no-relay lifecycle e2e because this host is not macOS.", flush=True)
    _append_runtime_checks(args, commands)

    if not commands:
        print("No desktop parity checks selected.", flush=True)
        return 0

    failures: list[tuple[str, int]] = []
    for label, command, env in commands:
        code = _run(label, command, env=env, dry_run=args.dry_run)
        if code != 0:
            failures.append((label, code))
            break
    if failures:
        label, code = failures[0]
        print(f"\nFAILED: {label} exited with {code}", flush=True)
        return code
    print("\nDesktop parity checks completed successfully.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
