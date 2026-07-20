#!/usr/bin/env python3
"""Validate Windows x64 desktop release installer payloads before publication."""
from __future__ import annotations

import argparse
import json
import re
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'desktop-tauri' / 'src-tauri' / 'python' / 'embedded_python_runtime_windows_x86_64_manifest.json'
PACKAGE_JSON = ROOT / 'desktop-tauri' / 'package.json'
PACKAGE_LOCK = ROOT / 'desktop-tauri' / 'package-lock.json'
TAURI_CONFIG = ROOT / 'desktop-tauri' / 'src-tauri' / 'tauri.conf.json'
PROVENANCE = 'embedded_python_runtime_provenance.json'
FORBIDDEN = re.compile(r'(^|[\\/])(cmake|ninja|nvcc|cl|msbuild)(\.exe)?$|cuda[-_]?toolkit|visual studio|(^|[\\/])buildtools([\\/]|$)|\.sln$|\.vcxproj$', re.I)

class ValidationError(RuntimeError):
    pass


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def expected_version_from_tag(tag: str | None, fallback: str) -> str:
    if not tag:
        return fallback
    match = re.fullmatch(r'desktop-v(\d+\.\d+\.\d+)', tag)
    if not match:
        raise ValidationError('release tag must match desktop-vX.Y.Z')
    return match.group(1)


def validate_config_versions(expected: str) -> None:
    package = _load_json(PACKAGE_JSON)
    lock = _load_json(PACKAGE_LOCK)
    tauri = _load_json(TAURI_CONFIG)
    values = {
        'package.json': package.get('version'),
        'package-lock.json': lock.get('version'),
        'tauri.conf.json': tauri.get('version'),
    }
    mismatches = {k: v for k, v in values.items() if v != expected}
    if mismatches:
        raise ValidationError(f'Windows release version mismatch: {mismatches}; expected {expected}')


def _sanitize_process_output(text: str, artifact: Path, dest: Path) -> str:
    sanitized = (text or '')[-4000:]
    for raw, replacement in (
        (str(artifact.resolve()), artifact.name),
        (str(dest.resolve()), dest.name),
        (str(artifact), artifact.name),
        (str(dest), dest.name),
    ):
        sanitized = sanitized.replace(raw, replacement)
    return sanitized[-1200:]


def _run_native_materializer(cmd: list[str], artifact: Path, dest: Path, *, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = _sanitize_process_output(exc.stdout or '', artifact, dest)
        raise ValidationError(f'native materialization timed out for {artifact.name}: {output}') from exc
    except subprocess.CalledProcessError as exc:
        output = _sanitize_process_output(exc.stdout or '', artifact, dest)
        raise ValidationError(f'native materialization failed for {artifact.name}: exit={exc.returncode}: {output}') from exc


def _cleanup_nsis_install(root: Path) -> None:
    uninstallers = sorted(
        p for p in root.rglob('*.exe')
        if p.is_file() and p.name.lower().startswith(('unins', 'uninstall'))
    )
    if not uninstallers:
        return
    artifact = uninstallers[0]
    try:
        _run_native_materializer([str(artifact.resolve()), '/S'], artifact, root, timeout=120)
    except ValidationError as exc:
        raise ValidationError(f'failed to clean NSIS install for {root.name}: {exc}') from exc


def _run_extract(artifact: Path, dest: Path):
    if artifact.is_dir():
        # Directory inputs are explicitly pre-materialized fixture/inspection trees,
        # not real MSI/NSIS installers.
        shutil.copytree(artifact, dest, dirs_exist_ok=True)
        return lambda: None
    if platform.system() != 'Windows':
        raise ValidationError(f'native Windows installer materialization is unavailable for {artifact.name}')
    artifact_abs = artifact.resolve()
    dest_abs = dest.resolve()
    suffix = artifact.suffix.lower()
    if suffix == '.msi':
        _run_native_materializer([
            'msiexec.exe',
            '/a',
            str(artifact_abs),
            '/qn',
            '/norestart',
            f'TARGETDIR={dest_abs}',
        ], artifact, dest)
        return lambda: None
    if suffix == '.exe':
        _run_native_materializer([str(artifact_abs), '/S', f'/D={dest_abs}'], artifact, dest)
        return lambda: _cleanup_nsis_install(dest_abs)
    raise ValidationError(f'unsupported Windows installer artifact type for {artifact.name}')


def _find_runtime(root: Path) -> Path:
    candidates = [p.parent for p in root.rglob('python.exe') if p.parent.name.lower() == 'python-runtime']
    if len(candidates) != 1:
        raise ValidationError(f'expected exactly one bundled python-runtime/python.exe, found {len(candidates)}')
    return candidates[0]


def _validate_provenance(runtime: Path, manifest: dict) -> None:
    path = runtime / PROVENANCE
    try:
        provenance = _load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError('missing or corrupt Windows runtime provenance') from exc
    wheel = provenance.get('llama_cpp_cuda_wheel') if isinstance(provenance.get('llama_cpp_cuda_wheel'), dict) else {}
    expected_wheel = manifest['llama_cpp_cuda_wheel']
    checks = {
        'runtime_id': provenance.get('runtime_id') == 'bundled-cpython-3.11-win-x86_64-cu124',
        'cpython_version': provenance.get('cpython_version') == manifest['cpython_version'] == '3.11.13',
        'target_triple': provenance.get('target_triple') == manifest['target_triple'] == 'x86_64-pc-windows-msvc',
        'archive_sha256': provenance.get('source_archive_sha256') == manifest['sha256'],
        'llama_name': wheel.get('name') == expected_wheel['name'],
        'llama_version': wheel.get('version') == '0.3.32',
        'llama_flavor': wheel.get('flavor') == 'cu124',
        'llama_sha256': wheel.get('sha256') == expected_wheel['sha256'],
        'packages': provenance.get('required_packages') == manifest['required_packages'],
        'wheels': provenance.get('python_package_wheels') == manifest.get('python_package_wheels', []),
        'dlls': provenance.get('required_native_dlls') == manifest['required_native_dlls'],
    }
    failed = sorted(k for k, ok in checks.items() if not ok)
    if failed:
        raise ValidationError(f'incomplete Windows runtime provenance fields: {failed}')
    closure = provenance.get('pe_dll_closure')
    if not isinstance(closure, list) or not closure:
        raise ValidationError('Windows runtime provenance must contain a non-empty PE DLL closure')
    names = {str(entry.get('name', '')).lower() for entry in closure if isinstance(entry, dict)}
    missing = {dll.lower() for dll in manifest['required_native_dlls']} - names
    if missing:
        raise ValidationError(f'PE DLL closure missing required entries: {sorted(missing)}')
    bad = [entry for entry in closure if not isinstance(entry, dict) or entry.get('machine') != 'IMAGE_FILE_MACHINE_AMD64']
    if bad:
        raise ValidationError('PE DLL closure contains non-AMD64 entries')


def _validate_runtime_tree(runtime: Path, manifest: dict) -> None:
    if not (runtime / 'python.exe').is_file():
        raise ValidationError('bundled python.exe is missing')
    names = {p.name.lower() for p in runtime.rglob('*') if p.is_file()}
    missing = {dll.lower() for dll in manifest['required_native_dlls']} - names
    if missing:
        raise ValidationError(f'bundled runtime missing required DLLs: {sorted(missing)}')
    forbidden = [p.relative_to(runtime).as_posix() for p in runtime.rglob('*') if p.is_file() and FORBIDDEN.search(p.relative_to(runtime).as_posix())]
    if forbidden:
        raise ValidationError(f'forbidden compiler/toolkit payload in Windows artifact: {forbidden[0]}')
    _validate_provenance(runtime, manifest)


def validate_artifact(artifact: Path, expected: str, kind: str, manifest: dict) -> None:
    if expected not in artifact.name:
        raise ValidationError(f'{kind} filename does not contain expected version {expected}: {artifact.name}')
    with tempfile.TemporaryDirectory(prefix=f'token-place-{kind}-') as td:
        dest = Path(td) / 'extract'
        dest.mkdir()
        cleanup = _run_extract(artifact, dest)
        try:
            runtime = _find_runtime(dest)
            _validate_runtime_tree(runtime, manifest)
        finally:
            cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--windows-nsis', type=Path, required=True)
    parser.add_argument('--windows-msi', type=Path, required=True)
    parser.add_argument('--expected-version', default=None)
    parser.add_argument('--release-tag', default=None)
    args = parser.parse_args(argv)
    manifest = _load_json(MANIFEST)
    expected = expected_version_from_tag(args.release_tag, args.expected_version or _load_json(PACKAGE_JSON).get('version'))
    validate_config_versions(expected)
    validate_artifact(args.windows_nsis, expected, 'NSIS', manifest)
    validate_artifact(args.windows_msi, expected, 'MSI', manifest)
    print(f'Windows desktop release artifacts validated for {expected}')
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Windows release artifact validation failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
