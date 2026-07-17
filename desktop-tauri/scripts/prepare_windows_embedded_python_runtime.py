"""Prepare the deterministic Windows x86_64 CPython + CUDA llama-cpp runtime."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC_TAURI = ROOT / "desktop-tauri" / "src-tauri"
MANIFEST = SRC_TAURI / "python" / "embedded_python_runtime_windows_manifest.json"
RUNTIME = SRC_TAURI / "python-runtime"
PROVENANCE = "embedded_python_runtime_provenance.json"
CACHE = Path(os.environ.get("TOKEN_PLACE_EMBEDDED_PYTHON_CACHE", Path.home() / ".cache" / "token-place" / "embedded-python"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, expected_sha256: str, dest: Path) -> None:
    if not expected_sha256 or expected_sha256.startswith("TO_BE_FILLED"):
        raise RuntimeError(f"missing immutable sha256 pin for {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        urllib.request.urlretrieve(url, dest)
    actual = _sha256(dest)
    if actual.lower() != expected_sha256.lower():
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch for {dest.name}: expected {expected_sha256}, got {actual}")


def _assert_wheel_flavor(path: Path, manifest: dict) -> None:
    wheel = manifest["llama_cpp_python_wheel"]
    if path.name != wheel["filename"]:
        raise RuntimeError(f"unexpected wheel filename: {path.name}")
    if "win_amd64" not in path.name or "0.3.32" not in path.name:
        raise RuntimeError(f"wrong Windows/version wheel flavor: {path.name}")
    if wheel.get("backend") != "cu124":
        raise RuntimeError("Windows runtime manifest must pin CUDA 12.4/cu124")


def main() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("Windows embedded runtime preparation must run on Windows")
    if platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError(f"unsupported Windows architecture: {platform.machine()}")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["target_triple"] != "x86_64-pc-windows-msvc":
        raise RuntimeError("wrong CPython target triple")
    if manifest["cpython_version"] != "3.11.13":
        raise RuntimeError("wrong CPython version")
    wheel_meta = manifest["llama_cpp_python_wheel"]
    if wheel_meta["version"] != manifest["required_packages"]["llama-cpp-python"]:
        raise RuntimeError("llama-cpp-python version mismatch in manifest")

    CACHE.mkdir(parents=True, exist_ok=True)
    py_archive = CACHE / Path(manifest["archive_url"].split("/")[-1].replace("%2B", "+"))
    wheel = CACHE / wheel_meta["filename"]
    _download(manifest["archive_url"], manifest["sha256"], py_archive)
    _download(wheel_meta["url"], wheel_meta["sha256"], wheel)
    _assert_wheel_flavor(wheel, manifest)

    staging = Path(tempfile.mkdtemp(prefix="token-place-win-runtime-"))
    try:
        with tarfile.open(py_archive, "r:gz") as tf:
            tf.extractall(staging)
        extracted = staging / manifest["expected_archive_root"]
        py = extracted / manifest["expected_interpreter_path"]
        if not py.is_file():
            raise RuntimeError("extracted CPython runtime missing python.exe")
        subprocess.run([str(py), "--version"], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "--no-index", "--find-links", str(wheel.parent), str(wheel)], check=True)
        req = SRC_TAURI / "python" / "requirements_desktop_runtime.txt"
        subprocess.run([str(py), "-m", "pip", "install", "--only-binary", ":all:", "-r", str(req)], check=True)
        probe = "import importlib.metadata as im,platform,sys; assert sys.version_info[:2]==(3,11); assert platform.machine().lower() in ('amd64','x86_64'); assert im.version('llama-cpp-python')=='0.3.32'"
        subprocess.run([str(py), "-c", probe], check=True)
        with zipfile.ZipFile(wheel) as zf:
            names = set(zf.namelist())
        required_dll_names = {name.lower() for name in manifest["required_native_dlls"] if name.lower().startswith(("llama", "ggml"))}
        present = {Path(name).name.lower() for name in names}
        missing = sorted(required_dll_names - present)
        if missing:
            raise RuntimeError(f"llama-cpp CUDA wheel missing native DLLs: {missing}")
        provenance = {
            "manifest": manifest,
            "python_archive_sha256": _sha256(py_archive),
            "llama_cpp_python_wheel_sha256": _sha256(wheel),
        }
        (extracted / PROVENANCE).write_text(json.dumps(provenance, indent=2, sort_keys=True), encoding="utf-8")
        shutil.rmtree(RUNTIME, ignore_errors=True)
        shutil.move(str(extracted), RUNTIME)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main()
