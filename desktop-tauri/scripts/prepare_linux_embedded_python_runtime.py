#!/usr/bin/env python3
"""Build the pinned Linux x86_64 CPU runtime in the application resource tree."""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import tempfile
from pathlib import Path

import prepare_embedded_python_runtime as shared

MANIFEST = shared.SRC_TAURI / "python/embedded_python_runtime_linux_x86_64_manifest.json"
RUNTIME_ID = "bundled-cpython-3.11-linux-x86_64-cpu"
PROFILE = "linux-x86_64-cpu-portable-v1"
STDLIB = ["encodings/__init__.py", "json/__init__.py", "os.py", "platform.py", "struct.py"]


def load_manifest(path: Path = MANIFEST) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    # Reuse the existing pinned schema/package validation without changing macOS.
    reference = shared.load_manifest()
    for key in (
        "schema_version",
        "cpython_version",
        "required_packages",
        "expected_archive_root",
        "expected_interpreter_path",
    ):
        if manifest.get(key) != reference[key]:
            raise shared.RuntimePrepError(f"invalid Linux manifest field: {key}")
    if (
        manifest.get("target_triple") != "x86_64-unknown-linux-gnu"
        or manifest.get("expected_architecture") != "x86_64"
        or manifest.get("expected_backend") != "cpu"
    ):
        raise shared.RuntimePrepError("only Linux x86_64 GNU CPU packaging is supported")
    digest = manifest.get("sha256", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise shared.RuntimePrepError("invalid Linux archive SHA-256")
    expected_url = (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{manifest['python_build_standalone_release']}/"
        f"{manifest['python_build_standalone_build'].replace('+', '%2B')}.tar.gz"
    )
    if manifest.get("archive_url") != expected_url or "latest" in expected_url:
        raise shared.RuntimePrepError("Linux archive must use the pinned upstream release")
    return manifest


def validate_runtime(runtime: Path, manifest: dict) -> dict:
    root = runtime.resolve()
    for relative in STDLIB:
        path = root / "lib/python3.11" / relative
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise shared.RuntimePrepError(f"missing or external standard library: {relative}")
    py = root / "bin/python3"
    shared.prove_interpreter(py, root, manifest)
    code = """
import importlib, importlib.metadata as metadata, json, pathlib, platform, sys
import encodings, os, struct
root = pathlib.Path(sys.prefix).resolve()
names = ['psutil', 'requests', 'dotenv', 'cryptography', 'jinja2', 'numpy', 'diskcache', 'llama_cpp']
for name in names:
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
import llama_cpp
assert not llama_cpp.llama_supports_gpu_offload(), 'CPU package unexpectedly supports GPU offload'
print(json.dumps({d.metadata['Name']: d.version for d in metadata.distributions()}))
"""
    packages = json.loads(shared.run([str(py), "-I", "-B", "-c", code]).stdout)
    for name, version in manifest["required_packages"].items():
        if packages.get(name) != version:
            raise shared.RuntimePrepError(f"wrong bundled package version: {name}")
    shared.run([str(py), "-I", "-B", "-m", "pip", "check"])
    return packages


def file_hashes(runtime: Path) -> dict[str, str]:
    result = {}
    for path in sorted(runtime.rglob("*")):
        if path.is_file() and path.name != shared.PROVENANCE:
            if not path.resolve().is_relative_to(runtime.resolve()):
                raise shared.RuntimePrepError(f"runtime file escapes package: {path}")
            result[path.relative_to(runtime).as_posix()] = shared.sha256(path)
    return result


def prepare(cache_dir: Path) -> None:
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise shared.RuntimePrepError("build requires a Linux x86_64 host")
    manifest = load_manifest()
    archive = shared.download_verified(manifest, cache_dir)
    with tempfile.TemporaryDirectory(
        prefix="linux-runtime-", dir=shared.OUTPUT.parent
    ) as directory:
        temporary = Path(directory)
        runtime = shared.extract_archive(archive, manifest, temporary)
        py = runtime / "bin/python3"
        shared.prove_interpreter(py, runtime, manifest)
        packages = [f"{name}=={version}" for name, version in manifest["required_packages"].items()]
        # Build a portable CPU backend; do not auto-detect the builder's ISA/GPU.
        shared.run(
            [str(py), "-I", "-m", "pip", "install", "--no-cache-dir", *packages],
            env={
                "CMAKE_ARGS": (
                    "-DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DGGML_VULKAN=OFF "
                    "-DGGML_BLAS=OFF -DGGML_AVX=OFF -DGGML_AVX2=OFF "
                    "-DGGML_FMA=OFF -DGGML_F16C=OFF -DGGML_BMI2=OFF"
                ),
                "FORCE_CMAKE": "1",
                "CMAKE_BUILD_PARALLEL_LEVEL": "2",
            },
        )
        installed = validate_runtime(runtime, manifest)
        shared.clean(runtime)
        data = shared.provenance(manifest, installed)
        data.update(
            expected_backend="cpu",
            build_profile=PROFILE,
            runtime_id=RUNTIME_ID,
            files=file_hashes(runtime),
        )
        (runtime / shared.PROVENANCE).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        backup = temporary / "old-runtime"
        if shared.OUTPUT.exists():
            shared.OUTPUT.rename(backup)
        runtime.rename(shared.OUTPUT)
        shutil.rmtree(backup, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir", type=Path, default=Path.home() / ".cache/token-place/embedded-python-linux"
    )
    args = parser.parse_args()
    prepare(args.cache_dir)
