#!/usr/bin/env python3
"""Prepare the relocatable macOS arm64 Python runtime for the Tauri app."""
from __future__ import annotations

import argparse, hashlib, json, os, shutil, subprocess, sys, tarfile, tempfile, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src-tauri" / "python" / "embedded_python_runtime_manifest.json"
OUT = ROOT / "src-tauri" / "python-runtime"
PROVENANCE = "token-place-runtime-provenance.json"

REQUIRED_IMPORTS = ["psutil", "requests", "dotenv", "cryptography", "jinja2", "numpy", "diskcache", "llama_cpp"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def load_manifest(path: Path = MANIFEST) -> dict:
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1:
        raise SystemExit("unsupported embedded runtime manifest schema_version")
    url = data.get("archive_url", "")
    if not url.startswith("https://") or "latest" in url:
        raise SystemExit("archive_url must be immutable HTTPS and must not use latest")
    digest = data.get("archive_sha256", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise SystemExit("archive_sha256 must be a lowercase SHA-256 hex digest")
    for key in ["cpython_version", "target_triple", "expected_archive_root", "expected_interpreter_path", "expected_architecture"]:
        if not data.get(key):
            raise SystemExit(f"manifest missing {key}")
    if data["target_triple"] != "aarch64-apple-darwin":
        raise SystemExit("embedded runtime manifest must target aarch64-apple-darwin")
    return data


def safe_members(tf: tarfile.TarFile, root: str):
    prefix = root.rstrip("/") + "/"
    for m in tf.getmembers():
        name = m.name
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or not (name == root or name.startswith(prefix)):
            raise SystemExit(f"unsafe or unexpected archive member: {name}")
        if m.islnk() or m.issym():
            target = Path(m.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise SystemExit(f"archive link escapes extraction root: {name}")
        yield m


def run(cmd, **kwargs):
    subprocess.run(cmd, check=True, **kwargs)


def probe_python(py: Path, manifest: dict):
    code = """
import json, platform, sys
print(json.dumps({"version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}", "major": sys.version_info.major, "minor": sys.version_info.minor, "machine": platform.machine(), "executable": sys.executable, "prefix": sys.prefix}))
"""
    out = subprocess.check_output([str(py), "-c", code], text=True)
    data = json.loads(out)
    if (data["major"], data["minor"]) != tuple(map(int, manifest["cpython_version"].split(".")[:2])):
        raise SystemExit(f"bundled Python version mismatch: {data['version']}")
    if data["machine"] != manifest["expected_architecture"]:
        raise SystemExit(f"bundled Python architecture mismatch: {data['machine']}")
    for key in ["executable", "prefix"]:
        if not Path(data[key]).resolve().is_relative_to(OUT.resolve()):
            raise SystemExit(f"{key} is outside runtime")


def provenance(manifest: dict) -> dict:
    pkgs = {}
    py = OUT / manifest["expected_interpreter_path"]
    if py.exists():
        code = "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))"
        try:
            pkgs = json.loads(subprocess.check_output([str(py), "-c", code], text=True))
        except Exception:
            pkgs = {}
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT.parent, text=True, capture_output=True).stdout.strip()
    return {"cpython_version": manifest["cpython_version"], "target_triple": manifest["target_triple"], "source_archive_sha256": manifest["archive_sha256"], "installed_packages": pkgs, "expected_backend": "metal", "build_timestamp": datetime.now(timezone.utc).isoformat(), "repository_commit": commit or None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", type=Path, default=Path(os.environ.get("TOKEN_PLACE_RUNTIME_CACHE", ROOT / ".cache" / "embedded-python")))
    ap.add_argument("--skip-install", action="store_true", help="for tests: only validate/extract/probe fake archives")
    args = ap.parse_args()
    m = load_manifest()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    archive = args.cache_dir / Path(urllib.request.urlparse(m["archive_url"]).path).name
    if not archive.exists() or sha256(archive) != m["archive_sha256"]:
        tmp = archive.with_suffix(".download")
        urllib.request.urlretrieve(m["archive_url"], tmp)  # nosec B310 - manifest validation requires immutable HTTPS URLs
        if sha256(tmp) != m["archive_sha256"]:
            tmp.unlink(missing_ok=True); raise SystemExit("embedded Python archive digest mismatch")
        tmp.replace(archive)

    with tempfile.TemporaryDirectory(prefix="token-place-python-runtime-") as td:
        td = Path(td)
        with tarfile.open(archive) as tf:
            tf.extractall(td, members=safe_members(tf, m["expected_archive_root"]))
        src = td / m["expected_archive_root"]
        py = src / m["expected_interpreter_path"]
        if not py.exists(): raise SystemExit("archive missing expected interpreter")
        staging = OUT.parent / f".{OUT.name}.staging"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(src, staging, symlinks=True)
        shutil.rmtree(OUT, ignore_errors=True)
        staging.replace(OUT)

    py = OUT / m["expected_interpreter_path"]
    probe_python(py, m)
    if not args.skip_install:
        env = os.environ.copy(); env.update({"PYTHONNOUSERSITE": "1", "CMAKE_ARGS": "-DGGML_METAL=on", "FORCE_CMAKE": "1"})
        run([str(py), "-m", "ensurepip", "--upgrade"], env=env)
        run([str(py), "-m", "pip", "install", "--upgrade", "pip"], env=env)
        req = ROOT / "src-tauri" / "python" / "requirements_desktop_runtime.txt"
        run([str(py), "-m", "pip", "install", "-r", str(req), "llama-cpp-python==0.3.32"], env=env)
        run([str(py), "-m", "pip", "check"], env=env)
        run([str(py), "-c", "\n".join(f"import {name}" for name in REQUIRED_IMPORTS)], env=env)
        probe = ROOT / "src-tauri" / "python" / "desktop_runtime_setup.py"
        run([str(py), str(probe), "probe", "--require-backend", "metal"], env=env)
    for pattern in ["**/__pycache__", "**/*.pyc", "**/tests", "**/test"]:
        for path in OUT.glob(pattern):
            if path.is_dir(): shutil.rmtree(path, ignore_errors=True)
            else: path.unlink(missing_ok=True)
    (OUT / PROVENANCE).write_text(json.dumps(provenance(m), indent=2, sort_keys=True) + "\n")

if __name__ == "__main__":
    main()
