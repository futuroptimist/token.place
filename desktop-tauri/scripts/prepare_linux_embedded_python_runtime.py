#!/usr/bin/env python3
"""Build the pinned Linux x86_64 CPU runtime in the application resource tree."""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import tempfile
from pathlib import Path

import prepare_embedded_python_runtime as shared

MANIFEST = shared.SRC_TAURI / "python/embedded_python_runtime_linux_x86_64_manifest.json"
RUNTIME_ID = "bundled-cpython-3.11-linux-x86_64-cpu"
PROFILE = "linux-x86_64-cpu-portable-v1"
STDLIB = ["encodings/__init__.py", "json/__init__.py", "os.py", "platform.py", "struct.py"]


def run_python(py: Path, args: list[str], **kwargs):
    # setup-python exports LD_LIBRARY_PATH. Never bind the bundled interpreter
    # to the runner's different libpython when preparing or validating it.
    env = kwargs.pop("env", {})
    env.update(LD_LIBRARY_PATH=str(py.parent.parent / "lib"), LD_PRELOAD="")
    return shared.run([str(py), "-I", "-B", *args], env=env, **kwargs)


def prove_interpreter(py: Path, root: Path, manifest: dict) -> None:
    code = (
        "import json,platform,sys; print(json.dumps({'version':platform.python_version(),"
        "'machine':platform.machine(),'executable':sys.executable,'prefix':sys.prefix}))"
    )
    data = json.loads(run_python(py, ["-c", code], timeout=30).stdout)
    if data["version"] != manifest["cpython_version"] or data["machine"] != "x86_64":
        raise shared.RuntimePrepError("bundled interpreter version/architecture mismatch")
    for key in ("executable", "prefix"):
        if not Path(data[key]).resolve().is_relative_to(root.resolve()):
            raise shared.RuntimePrepError(f"{key} is outside generated runtime")


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
        "runtime_notices",
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
    expected_build = (
        f"cpython-{manifest['cpython_version']}+{manifest['python_build_standalone_release']}"
        "-x86_64-unknown-linux-gnu-install_only"
    )
    if manifest.get("python_build_standalone_build") != expected_build:
        raise shared.RuntimePrepError("Linux archive build does not match target/version")
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
    prove_interpreter(py, root, manifest)
    code = """
import importlib, importlib.metadata as metadata, json, pathlib, platform, sys
import encodings, os, struct
root = pathlib.Path(sys.prefix).resolve()
assert tuple(sys.version_info[:3]) == (3, 11, 13), 'unexpected CPython version'
names = ['psutil', 'requests', 'dotenv', 'cryptography', 'jinja2', 'numpy', 'diskcache', 'llama_cpp']
for name in names:
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root), name
import llama_cpp
assert not llama_cpp.llama_supports_gpu_offload(), 'CPU package unexpectedly supports GPU offload'
print(json.dumps({d.metadata['Name']: d.version for d in metadata.distributions()}))
"""
    packages = json.loads(run_python(py, ["-c", code], timeout=90).stdout)
    normalized_packages = {
        re.sub(r"[-_.]+", "-", name).lower(): version for name, version in packages.items()
    }
    for name, version in manifest["required_packages"].items():
        if normalized_packages.get(re.sub(r"[-_.]+", "-", name).lower()) != version:
            raise shared.RuntimePrepError(f"wrong bundled package version: {name}")
    run_python(py, ["-m", "pip", "check"], timeout=90)
    return packages


def runtime_notices(manifest: dict) -> dict[str, str]:
    return {
        notice["path"]: (
            f"{notice['name']} redistribution notice: {notice['license']}\n"
            "See upstream distribution for complete license text.\n"
        )
        for notice in manifest["runtime_notices"]
    }


def write_runtime_notices(root: Path, manifest: dict) -> None:
    for name, text in runtime_notices(manifest).items():
        (root / name).write_text(text, encoding="utf-8")


def validate_runtime_notices(root: Path, manifest: dict) -> None:
    for name, text in runtime_notices(manifest).items():
        path = root / name
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            raise shared.RuntimePrepError(f"missing or invalid runtime notice: {name}")


def file_hashes(runtime: Path) -> dict[str, str | dict[str, str]]:
    result = {}
    root = runtime.resolve()
    for path in sorted(runtime.rglob("*")):
        relative = path.relative_to(runtime).as_posix()
        if path.is_symlink():
            try:
                target = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise shared.RuntimePrepError(f"broken or cyclic runtime link: {path}") from exc
            if not target.is_relative_to(root):
                raise shared.RuntimePrepError(f"runtime link escapes package: {path}")
            result[relative] = {"symlink": str(path.readlink())}
        elif path.is_file() and relative != shared.PROVENANCE:
            if not path.resolve().is_relative_to(root):
                raise shared.RuntimePrepError(f"runtime file escapes package: {path}")
            result[relative] = shared.sha256(path)
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
        runtime = shared.extract_archive(
            archive, manifest, temporary, allow_internal_parent_links=True
        )
        py = runtime / "bin/python3"
        prove_interpreter(py, runtime, manifest)
        packages = [f"{name}=={version}" for name, version in manifest["required_packages"].items()]
        # Build a portable CPU backend; do not auto-detect the builder's ISA/GPU.
        run_python(
            py,
            ["-m", "pip", "install", "--no-cache-dir", "--no-binary=llama-cpp-python", *packages],
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
        write_runtime_notices(runtime, manifest)
        validate_runtime_notices(runtime, manifest)
        data = shared.provenance(manifest, installed)
        data.update(
            expected_backend="cpu",
            build_profile=PROFILE,
            runtime_id=RUNTIME_ID,
            files=file_hashes(runtime),
        )
        (runtime / shared.PROVENANCE).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        # Recovery must survive staging cleanup even if both rename operations fail.
        backup_dir = None
        if shared.OUTPUT.exists():
            backup_dir = Path(
                tempfile.mkdtemp(prefix="linux-runtime-backup-", dir=shared.OUTPUT.parent)
            )
            backup = backup_dir / "old-runtime"
            try:
                shared.OUTPUT.rename(backup)
            except OSError:
                backup_dir.rmdir()
                raise
        try:
            runtime.rename(shared.OUTPUT)
        except OSError:
            if backup_dir is not None:
                try:
                    backup.rename(shared.OUTPUT)
                except OSError as exc:
                    raise shared.RuntimePrepError(
                        f"Runtime swap and restore failed; previous runtime preserved at {backup}. "
                        "Resolve the output-path conflict before restoring it; do not delete this backup."
                    ) from exc
                backup_dir.rmdir()
            raise
        if backup_dir is not None:
            shutil.rmtree(backup_dir)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir", type=Path, default=Path.home() / ".cache/token-place/embedded-python-linux"
    )
    args = parser.parse_args()
    prepare(args.cache_dir)
