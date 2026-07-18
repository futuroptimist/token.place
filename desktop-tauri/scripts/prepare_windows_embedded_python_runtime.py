#!/usr/bin/env python3
"""Prepare the deterministic Windows x86-64 CPython/CUDA runtime for Tauri."""
from __future__ import annotations

import argparse, hashlib, json, os, platform, re, shutil, subprocess, sys, tarfile, tempfile, urllib.request, zipfile
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = ROOT / "src-tauri"
MANIFEST = SRC_TAURI / "python" / "embedded_python_runtime_windows_x86_64_manifest.json"
OUTPUT = SRC_TAURI / "python-runtime"
PROVENANCE = "embedded_python_runtime_provenance.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_SYSTEM_DLLS = {"advapi32.dll", "bcrypt.dll", "crypt32.dll", "kernel32.dll", "msvcrt.dll", "ntdll.dll", "ole32.dll", "oleaut32.dll", "rpcrt4.dll", "sechost.dll", "shell32.dll", "shlwapi.dll", "user32.dll", "version.dll", "ws2_32.dll"}
FORBIDDEN_RUNTIME_PAYLOAD_RE = re.compile(r"(\.c$|\.cc$|\.cpp$|\.h$|\.hpp$|cmake|ninja|nvcc|cuda[-_]?toolkit|visual studio|msbuild)", re.I)

class RuntimePrepError(RuntimeError): pass

def sha256_file(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()

def load_manifest(path: Path=MANIFEST) -> dict:
    m=json.loads(path.read_text(encoding='utf-8'))
    required={"schema_version","cpython_version","archive_url","sha256","expected_archive_root","expected_interpreter_path","expected_architecture","llama_cpp_cuda_wheel","required_packages","required_native_dlls","target_triple"}
    missing=required-set(m)
    if missing: raise RuntimePrepError(f"windows runtime manifest missing keys: {sorted(missing)}")
    if m["schema_version"] != 1: raise RuntimePrepError("unsupported windows runtime manifest schema_version")
    if m.get("target_triple") != "x86_64-pc-windows-msvc": raise RuntimePrepError("windows runtime target_triple must be x86_64-pc-windows-msvc")
    wheel=m["llama_cpp_cuda_wheel"]
    if wheel.get("name") != "llama_cpp_python-0.3.32-py3-none-win_amd64.whl": raise RuntimePrepError("unexpected llama-cpp-python wheel name")
    if wheel.get("version") != "0.3.32" or wheel.get("flavor") != "cu124": raise RuntimePrepError("unexpected llama-cpp-python CUDA wheel version/flavor")
    if "win_amd64" not in wheel.get("name","") or "cu124" not in wheel.get("url",""): raise RuntimePrepError("CUDA wheel must be cu124 win_amd64")
    if m.get("expected_architecture") != "AMD64": raise RuntimePrepError("windows runtime architecture must be AMD64")
    if not SHA256_RE.fullmatch(m.get("sha256", "")): raise RuntimePrepError("archive sha256 must be 64 lowercase hex characters")
    if not SHA256_RE.fullmatch(wheel.get("sha256", "")): raise RuntimePrepError("llama-cpp-python wheel sha256 must be 64 lowercase hex characters")
    wheelhouse = m.get("python_package_wheels", [])
    if not isinstance(wheelhouse, list): raise RuntimePrepError("python_package_wheels must be a list")
    seen = set()
    for artifact in wheelhouse:
        for key in ("package", "version", "filename", "url", "sha256"):
            if key not in artifact: raise RuntimePrepError(f"python_package_wheels entry missing {key}")
        if not artifact["filename"].endswith(".whl"): raise RuntimePrepError("python_package_wheels entries must be wheels")
        if artifact["package"].lower().replace("_", "-") == "llama-cpp-python": raise RuntimePrepError("llama-cpp-python must be pinned only by llama_cpp_cuda_wheel")
        if not artifact["url"].startswith(("https://files.pythonhosted.org/", "https://github.com/")): raise RuntimePrepError("python wheel URLs must be immutable HTTPS artifact URLs")
        if not SHA256_RE.fullmatch(artifact["sha256"]): raise RuntimePrepError("python wheel sha256 must be 64 lowercase hex characters")
        if "win_amd64" not in artifact["filename"] and "none-any" not in artifact["filename"]: raise RuntimePrepError("python wheels must be win_amd64 or none-any")
        key = artifact["package"].lower().replace("_", "-")
        if key in seen: raise RuntimePrepError(f"duplicate python wheel package: {artifact['package']}")
        seen.add(key)
    bootstrap_packages = {"pip", "setuptools", "wheel"}
    required_wheel_packages = {k.lower().replace("_", "-") for k in m["required_packages"] if k.lower().replace("_", "-") not in ({"llama-cpp-python"} | bootstrap_packages)}
    missing_wheels = required_wheel_packages - seen
    extra_wheels = seen - required_wheel_packages
    if missing_wheels: raise RuntimePrepError(f"missing python wheel artifacts for required packages: {sorted(missing_wheels)}")
    if extra_wheels: raise RuntimePrepError(f"extra python wheel artifacts not in required_packages: {sorted(extra_wheels)}")
    return m

def fetch(url: str, sha: str, dest: Path) -> Path:
    if not url.startswith(("https://github.com/", "https://files.pythonhosted.org/")):
        raise RuntimePrepError("runtime artifacts must be fetched from pinned immutable HTTPS URLs")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        with urllib.request.urlopen(url, timeout=120) as r, dest.open('wb') as f:  # nosec B310 - URL scheme/host is validated and SHA-256 pinned
            shutil.copyfileobj(r, f)
    got=sha256_file(dest)
    if got != sha: raise RuntimePrepError(f"digest mismatch for {dest.name}: expected {sha} got {got}")
    return dest

def safe_extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, 'r:*') as tf:
        base=dest.resolve()
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                raise RuntimePrepError("archive member links are not allowed")
            if member.isdev() or member.isfifo():
                raise RuntimePrepError("archive member devices are not allowed")
            target=(dest/member.name).resolve()
            if not target.is_relative_to(base):
                raise RuntimePrepError("archive member escapes destination")
        tf.extractall(dest)  # nosec B202 - all members are resolved under dest before extraction

def validate_wheel(whl: Path, m: dict) -> None:
    wheel=m['llama_cpp_cuda_wheel']
    if whl.name != wheel['name']: raise RuntimePrepError("wrong wheel filename")
    with zipfile.ZipFile(whl) as zf:
        names=set(zf.namelist())
        if not any(n.endswith('.dist-info/METADATA') for n in names): raise RuntimePrepError("wheel missing METADATA")
        meta_name=next(n for n in names if n.endswith('.dist-info/METADATA'))
        meta=zf.read(meta_name).decode('utf-8','replace')
        if 'Name: llama_cpp_python' not in meta and 'Name: llama-cpp-python' not in meta: raise RuntimePrepError("wheel package name mismatch")
        if 'Version: 0.3.32' not in meta: raise RuntimePrepError("wheel version mismatch")
        wheel_name=next((n for n in names if n.endswith('.dist-info/WHEEL')), '')
        wheel_text=zf.read(wheel_name).decode('utf-8','replace') if wheel_name else ''
        if 'Tag: py3-none-win_amd64' not in wheel_text: raise RuntimePrepError("wheel tag must be py3-none-win_amd64")
        if not any(n.lower().endswith('llama.dll') for n in names): raise RuntimePrepError("wheel missing llama.dll native runtime")


def validate_installed_inventory(py: Path, m: dict) -> None:
    script = """
import importlib.metadata as md, json
print(json.dumps({dist.metadata['Name'].lower().replace('_','-'): dist.version for dist in md.distributions()}))
"""
    installed = json.loads(run([str(py), '-c', script]).stdout)
    expected = {k.lower().replace('_', '-'): v for k, v in m['required_packages'].items()}
    missing = {k: v for k, v in expected.items() if installed.get(k) != v}
    if missing:
        raise RuntimePrepError(f"installed package inventory mismatch: {sorted(missing)}")
    unmanaged = sorted(k for k in installed if k not in expected and k not in {'pip', 'setuptools', 'wheel'})
    if unmanaged:
        raise RuntimePrepError(f"unexpected installed packages in bundled runtime: {unmanaged}")
    run([str(py), '-m', 'pip', 'check', '--disable-pip-version-check'])


def validate_runtime_payload(runtime: Path, m: dict) -> None:
    required = {dll.lower() for dll in m['required_native_dlls']}
    present = {p.name.lower() for p in runtime.rglob('*') if p.is_file()}
    missing = sorted(required - present)
    if missing:
        raise RuntimePrepError(f"missing required DLL: {missing[0]}")
    forbidden = [p.relative_to(runtime).as_posix() for p in runtime.rglob('*') if p.is_file() and FORBIDDEN_RUNTIME_PAYLOAD_RE.search(p.name)]
    if forbidden:
        raise RuntimePrepError(f"forbidden compiler/toolkit/source payload in bundled runtime: {forbidden[0]}")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)

def write_provenance(runtime: Path, m: dict) -> None:
    payload={
        'runtime_id': 'bundled-cpython-3.11-win-x86_64-cu124',
        'cpython_version': m['cpython_version'], 'target_triple': m['target_triple'],
        'source_archive_sha256': m['sha256'], 'llama_cpp_cuda_wheel': m['llama_cpp_cuda_wheel'],
        'required_packages': m['required_packages'], 'required_native_dlls': m['required_native_dlls'],
        'expected_backend': 'cuda', 'build_timestamp': provenance_timestamp(),
        'python_package_wheels': m.get('python_package_wheels', []), 'pe_dll_closure': m.get('pe_dll_closure', []),
    }
    (runtime/PROVENANCE).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')

def normalize_windows_x86_64_arch(machine: str) -> str:
    arch=machine.strip().lower().replace('-', '_')
    if arch in {"amd64", "x64", "x86_64"}:
        return "x86_64"
    return arch

def validate_host_architecture() -> None:
    if normalize_windows_x86_64_arch(platform.machine()) != "x86_64":
        raise RuntimePrepError("windows embedded runtime preparation requires an x86-64 host")

def provenance_timestamp() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch is not None:
        return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()
    return "reproducible-build-timestamp-unset"

def fetch_wheelhouse(m: dict, cache: Path) -> list[Path]:
    wheels = []
    for artifact in m.get("python_package_wheels", []):
        wheels.append(fetch(artifact["url"], artifact["sha256"], cache / artifact["filename"]))
    return wheels

def write_hash_requirements(path: Path, m: dict) -> list[str]:
    artifacts = {a["package"].lower().replace("_", "-"): a for a in m.get("python_package_wheels", [])}
    lines = []
    for package, version in sorted(m["required_packages"].items()):
        key = package.lower().replace("_", "-")
        if key == "llama-cpp-python":
            continue
        artifact = artifacts.get(key)
        if artifact is None:
            raise RuntimePrepError(f"missing wheel artifact for required package: {package}")
        if artifact["version"] != version:
            raise RuntimePrepError(f"wheel artifact version mismatch for {package}")
        lines.append(f"{package}=={version} --hash=sha256:{artifact['sha256']}")
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return lines

def prepare(m: dict) -> None:
    validate_host_architecture()
    cache=ROOT/'.cache'/'windows-python-runtime'
    archive=fetch(m['archive_url'], m['sha256'], cache/Path(m['archive_url'].split('/')[-1].replace('%2B','+')).name)
    wheel_meta=m['llama_cpp_cuda_wheel']
    wheel=fetch(wheel_meta['url'], wheel_meta['sha256'], cache/wheel_meta['name'])
    wheelhouse=fetch_wheelhouse(m, cache)
    validate_wheel(wheel, m)
    with tempfile.TemporaryDirectory(prefix='token-place-win-python-', dir=str(OUTPUT.parent)) as td:
        tmp=Path(td); safe_extract_tar(archive, tmp)
        staged=tmp/'python-runtime'; shutil.move(str(tmp/m['expected_archive_root']), staged)
        py=staged/m['expected_interpreter_path']
        if not py.is_file(): raise RuntimePrepError('archive missing python.exe')
        requirements=tmp/'requirements-windows-runtime.txt'
        baseline=write_hash_requirements(requirements, m)
        if baseline:
            run([str(py), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir', '--no-index', '--only-binary', ':all:', '--require-hashes', '--find-links', str(cache), '-r', str(requirements)])
        run([str(py), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir', '--no-index', '--only-binary', ':all:', '--no-deps', str(wheel)])
        expected_version=[int(part) for part in m['cpython_version'].split('.')[:3]]
        data=json.loads(run([str(py), '-c', "import json,platform,sys; print(json.dumps({'version':list(sys.version_info[:3]),'machine':platform.machine()}))"]).stdout)
        if data != {'version':expected_version, 'machine':'AMD64'}: raise RuntimePrepError(f'interpreter probe mismatch: {data}')
        validate_installed_inventory(py, m)
        validate_runtime_payload(staged, m)
        for notice in m.get('runtime_notices',[]): (staged/notice['path']).write_text(f"{notice['name']} redistribution notice: {notice['license']}\n", encoding='utf-8')
        write_provenance(staged, m)
        backup=tmp/'old-runtime'
        if OUTPUT.exists(): OUTPUT.rename(backup)
        try:
            staged.rename(OUTPUT)
        except BaseException:
            if backup.exists() and not OUTPUT.exists():
                backup.rename(OUTPUT)
            raise

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument('--manifest', type=Path, default=MANIFEST); ap.add_argument('--check-manifest-only', action='store_true')
    args=ap.parse_args()
    try:
        m=load_manifest(args.manifest)
        if not args.check_manifest_only: prepare(m)
        return 0
    except Exception as e:
        print(f"windows embedded runtime preparation failed: {e}", file=sys.stderr); return 1
if __name__ == '__main__': raise SystemExit(main())
