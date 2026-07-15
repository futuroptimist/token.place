#!/usr/bin/env python3
"""Prepare the self-contained macOS arm64 desktop Python runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "desktop-tauri" / "src-tauri" / "python" / "embedded_python_runtime_manifest.json"
OUT = REPO / "desktop-tauri" / "src-tauri" / "python-runtime"
REQ = REPO / "desktop-tauri" / "src-tauri" / "python" / "requirements_desktop_runtime.txt"
PROVENANCE = "embedded_runtime_provenance.json"
IMPORTS = ["psutil", "requests", "dotenv", "cryptography", "jinja2", "numpy", "diskcache", "llama_cpp"]


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise SystemExit("unsupported embedded runtime manifest schema_version")
    parsed = urlparse(data.get("archive_url", ""))
    if parsed.scheme != "https" or not parsed.netloc:
        raise SystemExit("embedded runtime archive_url must be immutable HTTPS")
    digest = data.get("archive_sha256", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise SystemExit("embedded runtime archive_sha256 is missing or malformed")
    required = ["cpython_version", "python_build_standalone_build", "target_triple", "expected_archive_root", "expected_interpreter_path", "expected_architecture"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise SystemExit(f"embedded runtime manifest missing fields: {', '.join(missing)}")
    return data


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download_verified(manifest: dict, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(manifest["archive_url"]).path).name.replace("%2B", "+")
    archive = cache_dir / filename
    if not archive.exists() or sha256(archive) != manifest["archive_sha256"]:
        tmp = archive.with_suffix(archive.suffix + ".tmp")
        # nosec B310 - manifest validation rejects non-HTTPS URLs before this download.
        with urllib.request.urlopen(manifest["archive_url"], timeout=120) as response, tmp.open("wb") as fh:  # nosec B310
            shutil.copyfileobj(response, fh)
        if sha256(tmp) != manifest["archive_sha256"]:
            tmp.unlink(missing_ok=True)
            raise SystemExit("embedded runtime archive digest mismatch")
        tmp.replace(archive)
    if sha256(archive) != manifest["archive_sha256"]:
        raise SystemExit("cached embedded runtime archive digest mismatch")
    return archive


def safe_extract(archive: Path, dest: Path, expected_root: str) -> Path:
    with tarfile.open(archive, "r:gz") as tf:
        root = dest.resolve()
        for member in tf.getmembers():
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise SystemExit(f"unsafe archive member path: {member.name}")
            target = (dest / member.name).resolve()
            if not str(target).startswith(str(root) + os.sep) and target != root:
                raise SystemExit(f"archive member escapes extraction root: {member.name}")
            if member.issym() or member.islnk():
                link_target = Path(member.linkname)
                resolved = (target.parent / link_target).resolve() if not link_target.is_absolute() else link_target.resolve()
                if not str(resolved).startswith(str(root) + os.sep):
                    raise SystemExit(f"archive link escapes extraction root: {member.name}")
        tf.extractall(dest, filter="data")  # nosec B202 - members are prevalidated for traversal/link escapes
    extracted_root = dest / expected_root
    if not extracted_root.is_dir():
        raise SystemExit("embedded runtime archive root layout is unexpected")
    return extracted_root


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    env = kwargs.pop("env", os.environ.copy())
    env.setdefault("PYTHONNOUSERSITE", "1")
    return subprocess.run(cmd, check=True, text=True, env=env, **kwargs)


def validate_python(python: Path, manifest: dict) -> None:
    probe = "import json,platform,sys; print(json.dumps({'version': sys.version.split()[0], 'machine': platform.machine(), 'executable': sys.executable, 'prefix': sys.prefix}))"
    out = run([str(python), "-c", probe], stdout=subprocess.PIPE).stdout
    data = json.loads(out)
    if not data["version"].startswith("3.11.") or data["machine"] != manifest["expected_architecture"]:
        raise SystemExit("embedded runtime interpreter version or architecture mismatch")
    for key in ("executable", "prefix"):
        if not Path(data[key]).resolve().is_relative_to(OUT.resolve()):
            raise SystemExit(f"embedded runtime {key} is not inside generated runtime")


def installed_versions(python: Path) -> dict:
    code = "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}, sort_keys=True))"
    return json.loads(run([str(python), "-c", code], stdout=subprocess.PIPE).stdout)


def cleanup_runtime(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_dir() and path.name in {"__pycache__", "test", "tests", "testing"}:
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() and (path.suffix == ".pyc" or path.name.endswith(".pyo")):
            path.unlink(missing_ok=True)


def write_provenance(python: Path, manifest: dict) -> None:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
    data = {
        "cpython_version": manifest["cpython_version"],
        "target_triple": manifest["target_triple"],
        "source_archive_sha256": manifest["archive_sha256"],
        "installed_package_versions": installed_versions(python),
        "expected_backend": "metal",
        "build_timestamp": datetime.now(timezone.utc).isoformat(),
        "repository_commit": commit or None,
    }
    (OUT / PROVENANCE).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def existing_runtime_valid(manifest: dict) -> bool:
    python = OUT / manifest["expected_interpreter_path"]
    provenance = OUT / PROVENANCE
    if not python.is_file() or not provenance.is_file():
        return False
    try:
        data = json.loads(provenance.read_text(encoding="utf-8"))
        return data.get("source_archive_sha256") == manifest["archive_sha256"] and validate_python(python, manifest) is None
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "token-place" / "embedded-python")
    args = parser.parse_args()
    manifest = load_manifest(MANIFEST)
    if existing_runtime_valid(manifest):
        print(f"embedded runtime already valid at {OUT}")
        return
    archive = download_verified(manifest, args.cache_dir)
    with tempfile.TemporaryDirectory(prefix="token-place-python-runtime-") as tmp_s:
        tmp = Path(tmp_s)
        extracted = safe_extract(archive, tmp / "extract", manifest["expected_archive_root"])
        staged = tmp / "python-runtime.new"
        shutil.copytree(extracted, staged, symlinks=True)
        final_python = staged / manifest["expected_interpreter_path"]
        if not final_python.exists():
            raise SystemExit("embedded runtime interpreter is missing after layout normalization")
        if OUT.exists():
            shutil.rmtree(OUT)
        staged.replace(OUT)
    python = OUT / manifest["expected_interpreter_path"]
    validate_python(python, manifest)
    run([str(python), "-m", "ensurepip", "--upgrade"])
    run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(python), "-m", "pip", "install", "-r", str(REQ), "numpy", "diskcache"])
    sys.path.insert(0, str(REPO / "desktop-tauri" / "src-tauri" / "python"))
    from desktop_gpu_packaging import llama_cpp_install_plan
    plan = llama_cpp_install_plan(platform="darwin", requirements_path=REPO / "requirements.txt")
    env = os.environ.copy(); env.update(plan.pip_env())
    run([str(python), "-m", "pip", "install", plan.package_spec, *plan.pip_install_args()], env=env)
    run([str(python), "-m", "pip", "check"])
    run([str(python), "-c", ";".join(f"import {name}" for name in IMPORTS)])
    probe = REPO / "desktop-tauri" / "scripts" / "verify_desktop_runtime.py"
    if probe.exists():
        completed = run([str(python), str(probe), "--mode", "gpu", "--json"], stdout=subprocess.PIPE)
        if '"backend": "metal"' not in completed.stdout and '"backend":"metal"' not in completed.stdout:
            raise SystemExit("embedded runtime probe did not report Metal backend")
    cleanup_runtime(OUT)
    write_provenance(python, manifest)
    print(f"prepared embedded runtime at {OUT}")


if __name__ == "__main__":
    main()
