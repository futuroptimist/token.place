#!/usr/bin/env python3
"""Helpers for running the token.place test suite inside a container."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = os.environ.get("CONTAINER_RUNTIME", "docker")
ALLOWED_RUNTIMES = {"docker", "podman"}
DEFAULT_IMAGE = os.environ.get("CONTAINER_TEST_IMAGE", "token.place-test-runner:latest")
DOCKERFILE_RELATIVE = Path("docker/test-runner.Dockerfile")
WORKDIR = "/workspace"


def _normalise_runtime(runtime: str) -> str:
    runtime = runtime.lower()
    if runtime not in ALLOWED_RUNTIMES:
        raise SystemExit(
            f"Unsupported container runtime '{runtime}'. Choose one of: {', '.join(sorted(ALLOWED_RUNTIMES))}."
        )
    return runtime


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime",
        default=DEFAULT_RUNTIME,
        help="Container runtime to invoke (docker or podman). Defaults to CONTAINER_RUNTIME or docker.",
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=(
            "Tag to use for the built container image. Defaults to CONTAINER_TEST_IMAGE or "
            "token.place-test-runner:latest."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the container command as JSON without executing docker/podman.",
    )
    parser.add_argument(
        "extra",
        nargs=argparse.REMAINDER,
        help="Optional arguments forwarded to ./run_all_tests.sh. Use '-- <args>' to pass them.",
    )
    return parser.parse_args(argv)


def _build_run_command(runtime: str, image: str, forwarded: list[str]) -> list[str]:
    mount = f"{PROJECT_ROOT}:/workspace"
    command = [
        runtime,
        "run",
        "--rm",
        "-t",
        "-v",
        mount,
        "-w",
        WORKDIR,
        image,
        "./run_all_tests.sh",
    ]
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]
    if forwarded:
        command.extend(forwarded)
    return command


def _build_spec(runtime: str, image: str, forwarded: list[str]) -> dict[str, object]:
    dockerfile = PROJECT_ROOT / DOCKERFILE_RELATIVE
    return {
        "runtime": runtime,
        "image": image,
        "dockerfile": str(DOCKERFILE_RELATIVE),
        "context": str(PROJECT_ROOT),
        "workdir": WORKDIR,
        "command": _build_run_command(runtime, image, forwarded),
        "dockerfile_exists": dockerfile.exists(),
    }


def _ensure_runtime_available(runtime: str) -> None:
    if shutil.which(runtime) is None:
        raise SystemExit(
            f"Container runtime '{runtime}' not found on PATH. Install it or run with --dry-run."
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    runtime = _normalise_runtime(args.runtime)
    image = args.image
    forwarded = list(args.extra or [])

    spec = _build_spec(runtime, image, forwarded)

    if args.dry_run:
        json.dump(spec, sys.stdout)
        sys.stdout.write("\n")
        return 0

    dockerfile_path = PROJECT_ROOT / DOCKERFILE_RELATIVE
    if not dockerfile_path.exists():
        raise SystemExit(f"Dockerfile not found at {dockerfile_path}")

    _ensure_runtime_available(runtime)

    build_cmd = [
        runtime,
        "build",
        "-f",
        str(dockerfile_path),
        "-t",
        image,
        str(PROJECT_ROOT),
    ]

    run_cmd = spec["command"]

    subprocess.run(build_cmd, check=True)
    subprocess.run(run_cmd, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
