#!/usr/bin/env python3
"""Prepare the deterministic Windows x86-64 CPython/CUDA runtime for Tauri."""
from __future__ import annotations

import argparse, hashlib, json, os, platform, re, shutil, struct, subprocess, sys, tarfile, tempfile, urllib.request, zipfile
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = ROOT / "src-tauri"
MANIFEST = SRC_TAURI / "python" / "embedded_python_runtime_windows_x86_64_manifest.json"
OUTPUT = SRC_TAURI / "python-runtime"
PROVENANCE = "embedded_python_runtime_provenance.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_SYSTEM_DLLS = {
    "advapi32.dll", "bcrypt.dll", "crypt32.dll", "gdi32.dll", "kernel32.dll",
    "msvcrt.dll", "netapi32.dll", "ntdll.dll", "ole32.dll", "oleaut32.dll", "rpcrt4.dll",
    "sechost.dll", "shell32.dll", "shlwapi.dll", "ucrtbase.dll", "user32.dll",
    "version.dll", "ws2_32.dll",
    # Driver-provided and intentionally external; CUDA runtime/cuBLAS/OpenSSL/MSVC
    # payloads must be bundled and validated as part of the runtime closure.
    "nvcuda.dll",
}
WINDOWS_API_SET_DLL_RE = re.compile(r"^(api|ext)-ms-win-[a-z0-9-]+-l[0-9]+-[0-9]+-[0-9]+\.dll$", re.I)
FORBIDDEN_RUNTIME_PAYLOAD_RE = re.compile(r"(^|[\\/])(cmake|ninja|nvcc|cl|msbuild)(\.exe)?$|cuda[-_]?toolkit|visual studio|(^|[\\/])buildtools([\\/]|$)|\.sln$|\.vcxproj$", re.I)
DISTLIB_UNUSED_NON_X64_LAUNCHERS = {"t32.exe", "w32.exe", "t64-arm.exe", "w64-arm.exe"}

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

def is_windows_system_dll(name: str) -> bool:
    dll = name.lower()
    return dll in WINDOWS_SYSTEM_DLLS or bool(WINDOWS_API_SET_DLL_RE.fullmatch(dll))

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


def validate_python_package_wheel(whl: Path, artifact: dict) -> None:
    with zipfile.ZipFile(whl) as zf:
        names = set(zf.namelist())
        meta_name = next((n for n in names if n.endswith('.dist-info/METADATA')), '')
        wheel_name = next((n for n in names if n.endswith('.dist-info/WHEEL')), '')
        if not meta_name or not wheel_name:
            raise RuntimePrepError(f"python wheel missing METADATA/WHEEL: {whl.name}")
        meta = zf.read(meta_name).decode('utf-8', 'replace')
        wheel_text = zf.read(wheel_name).decode('utf-8', 'replace')
    expected_name = artifact['package'].lower().replace('_', '-')
    found_name = ''
    found_version = ''
    tags = []
    for line in meta.splitlines():
        if line.lower().startswith('name:'):
            found_name = line.split(':', 1)[1].strip().lower().replace('_', '-')
        elif line.lower().startswith('version:'):
            found_version = line.split(':', 1)[1].strip()
    for line in wheel_text.splitlines():
        if line.lower().startswith('tag:'):
            tags.append(line.split(':', 1)[1].strip())
    if found_name != expected_name or found_version != artifact['version']:
        raise RuntimePrepError(f"python wheel metadata mismatch: {whl.name}")
    if not any(tag.endswith('win_amd64') or tag.endswith('none-any') for tag in tags):
        raise RuntimePrepError(f"python wheel tag must be win_amd64 or none-any: {whl.name}")


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


def validate_runtime_payload(runtime: Path, m: dict) -> list[dict[str, object]]:
    required = {dll.lower() for dll in m['required_native_dlls']}
    present = {p.name.lower() for p in runtime.rglob('*') if p.is_file()}
    missing = sorted(required - present)
    if missing:
        raise RuntimePrepError(f"missing required DLL: {missing[0]}")
    forbidden = [
        p.relative_to(runtime).as_posix()
        for p in runtime.rglob('*')
        if p.is_file() and FORBIDDEN_RUNTIME_PAYLOAD_RE.search(p.relative_to(runtime).as_posix())
    ]
    if forbidden:
        raise RuntimePrepError(f"forbidden compiler/toolkit/source payload in bundled runtime: {forbidden[0]}")
    return validate_pe_dll_closure(runtime, m)


def _rva_to_offset(sections: list[tuple[int, int, int, int]], rva: int) -> int | None:
    for virtual_address, virtual_size, raw_pointer, raw_size in sections:
        size = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + size:
            return raw_pointer + (rva - virtual_address)
    return None


def inspect_pe(path: Path, display_name: str | None = None) -> tuple[str, list[str]]:
    label = display_name or path.name
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b'MZ':
        raise RuntimePrepError(f'not a PE file: {label}')
    pe_offset = struct.unpack_from('<I', data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset:pe_offset + 4] != b'PE\0\0':
        raise RuntimePrepError(f'invalid PE header: {label}')
    machine = struct.unpack_from('<H', data, pe_offset + 4)[0]
    if machine != 0x8664:
        if machine == 0xAA64:
            raise RuntimePrepError(f'ARM64 PE payload rejected: {label}')
        if machine == 0x014C:
            raise RuntimePrepError(f'x86 PE payload rejected: {label}')
        raise RuntimePrepError(f'unsupported PE machine 0x{machine:04x}: {label}')
    section_count = struct.unpack_from('<H', data, pe_offset + 6)[0]
    optional_size = struct.unpack_from('<H', data, pe_offset + 20)[0]
    optional_offset = pe_offset + 24
    magic = struct.unpack_from('<H', data, optional_offset)[0]
    if magic == 0x20B:
        data_directory_offset = optional_offset + 112
        image_base = struct.unpack_from('<Q', data, optional_offset + 24)[0]
    elif magic == 0x10B:
        data_directory_offset = optional_offset + 96
        image_base = struct.unpack_from('<I', data, optional_offset + 28)[0]
    else:
        raise RuntimePrepError(f'unsupported PE optional header: {label}')
    import_rva = 0
    delay_import_rva = 0
    if data_directory_offset + 8 * 2 <= len(data):
        import_rva = struct.unpack_from('<II', data, data_directory_offset + 8)[0]
    if data_directory_offset + 8 * 14 <= len(data):
        delay_import_rva = struct.unpack_from('<II', data, data_directory_offset + 8 * 13)[0]
    sections = []
    section_offset = optional_offset + optional_size
    for index in range(section_count):
        off = section_offset + index * 40
        if off + 40 > len(data):
            break
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from('<IIII', data, off + 8)
        sections.append((virtual_address, virtual_size, raw_pointer, raw_size))
    imports: list[str] = []

    def pointer_to_rva(value: int, *, delay_attrs: int | None = None) -> int:
        if delay_attrs is None or (delay_attrs & 1) or _rva_to_offset(sections, value) is not None:
            return value
        if value >= image_base:
            return value - image_base
        return value

    def read_import_descriptors(rva: int, stride: int, name_index: int, *, delay: bool = False) -> None:
        if not rva:
            return
        desc_offset = _rva_to_offset(sections, rva)
        while desc_offset is not None and desc_offset + stride <= len(data):
            fields = struct.unpack_from('<' + 'I' * (stride // 4), data, desc_offset)
            if not any(fields):
                break
            delay_attrs = fields[0] if delay else None
            name_rva = pointer_to_rva(fields[name_index], delay_attrs=delay_attrs)
            name_offset = _rva_to_offset(sections, name_rva)
            if name_offset is None:
                raise RuntimePrepError(f'unresolved PE import name in {label}')
            end = data.find(b'\0', name_offset)
            if end < 0:
                raise RuntimePrepError(f'unterminated PE import name in {label}')
            imports.append(data[name_offset:end].decode('ascii', 'replace').lower())
            desc_offset += stride

    read_import_descriptors(import_rva, 20, 3)
    read_import_descriptors(delay_import_rva, 32, 1, delay=True)
    return 'IMAGE_FILE_MACHINE_AMD64', imports

def _resolve_import_target(importer: Path, dll: str, candidates: dict[str, list[Path]], runtime: Path) -> Path | None:
    matches = candidates.get(dll.lower(), [])
    if not matches:
        return None
    same_dir = [p for p in matches if p.parent == importer.parent]
    pool = same_dir or matches
    if len(pool) == 1:
        return pool[0]
    digests = {sha256_file(p) for p in pool}
    if len(digests) == 1:
        return sorted(pool, key=lambda p: p.relative_to(runtime).as_posix().lower())[0]
    rels = [p.relative_to(runtime).as_posix() for p in pool]
    raise RuntimePrepError(f"ambiguous duplicate DLL basename: {dll.lower()} ({sorted(rels)})")


def validate_pe_dll_closure(runtime: Path, m: dict) -> list[dict[str, object]]:
    pe_files = sorted(
        (p for p in runtime.rglob('*') if p.is_file() and p.suffix.lower() in {'.exe', '.dll', '.pyd'}),
        key=lambda item: item.relative_to(runtime).as_posix().lower(),
    )
    candidates: dict[str, list[Path]] = {}
    for p in pe_files:
        candidates.setdefault(p.name.lower(), []).append(p)
    queue = list(pe_files)
    seen: set[str] = set()
    closure: list[dict[str, object]] = []
    unresolved: list[str] = []
    while queue:
        pe = queue.pop(0)
        rel = pe.relative_to(runtime).as_posix()
        key = rel.lower()
        if key in seen:
            continue
        seen.add(key)
        machine, imports = inspect_pe(pe, rel)
        closure.append({'name': pe.name, 'path': rel, 'machine': machine, 'imports': sorted(imports), 'sha256': sha256_file(pe)})
        for dll in imports:
            if is_windows_system_dll(dll):
                continue
            target = _resolve_import_target(pe, dll, candidates, runtime)
            if target is None:
                unresolved.append(f'{dll} required by {rel}')
                continue
            queue.append(target)
    if unresolved:
        bounded = sorted(set(unresolved))[:25]
        suffix = '' if len(set(unresolved)) <= 25 else f' (and {len(set(unresolved)) - 25} more)'
        raise RuntimePrepError(f"unresolved non-system DLL imports: {bounded}{suffix}")
    required = {dll.lower() for dll in m.get('required_native_dlls', [])}
    found_names = {entry['name'].lower() for entry in closure}
    found_paths = {entry['path'].lower() for entry in closure}
    missing = sorted(name for name in required if name not in found_names and name not in found_paths)
    if missing:
        raise RuntimePrepError(f'missing required PE DLL closure entry: {missing[0]}')
    expected = {str(entry.get('path') or entry.get('name', '')).lower() for entry in m.get('pe_dll_closure', []) if isinstance(entry, dict)}
    if expected and not expected.issubset(found_paths | found_names):
        raise RuntimePrepError(f'packaged PE closure incomplete: {sorted(expected - (found_paths | found_names))}')
    if not closure:
        raise RuntimePrepError('packaged PE closure must be non-empty')
    return closure

def _is_distlib_launcher_resource(path: Path, runtime: Path) -> bool:
    rel_parts = tuple(part.lower() for part in path.relative_to(runtime).parts)
    return 'distlib' in rel_parts and ('pip' in rel_parts or rel_parts[-2:] == ('distlib', path.name.lower()))


def prune_distlib_unused_non_x64_launchers(runtime: Path) -> list[str]:
    removed: list[str] = []
    for path in sorted(runtime.rglob('*.exe'), key=lambda p: p.relative_to(runtime).as_posix().lower()):
        if path.name.lower() in DISTLIB_UNUSED_NON_X64_LAUNCHERS and _is_distlib_launcher_resource(path, runtime):
            removed.append(path.relative_to(runtime).as_posix())
            path.unlink()
    return removed

def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)

def write_provenance(runtime: Path, m: dict, pe_closure: list[dict[str, object]] | None = None) -> None:
    payload={
        'runtime_id': 'bundled-cpython-3.11-win-x86_64-cu124',
        'cpython_version': m['cpython_version'], 'target_triple': m['target_triple'],
        'source_archive_sha256': m['sha256'], 'llama_cpp_cuda_wheel': m['llama_cpp_cuda_wheel'],
        'required_packages': m['required_packages'], 'required_native_dlls': m['required_native_dlls'],
        'expected_backend': 'cuda', 'build_timestamp': provenance_timestamp(),
        'python_package_wheels': m.get('python_package_wheels', []), 'pe_dll_closure': pe_closure if pe_closure is not None else m.get('pe_dll_closure', []),
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
        wheel = fetch(artifact["url"], artifact["sha256"], cache / artifact["filename"])
        validate_python_package_wheel(wheel, artifact)
        wheels.append(wheel)
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
        prune_distlib_unused_non_x64_launchers(staged)
        pe_closure=validate_runtime_payload(staged, m)
        for notice in m.get('runtime_notices',[]): (staged/notice['path']).write_text(f"{notice['name']} redistribution notice: {notice['license']}\n", encoding='utf-8')
        write_provenance(staged, m, pe_closure)
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
