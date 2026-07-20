#!/usr/bin/env python3
"""Validate Windows x64 desktop release installer payloads before publication."""
from __future__ import annotations

import argparse
import json
import os
import re
import platform
import shutil
import tomllib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'desktop-tauri' / 'src-tauri' / 'python' / 'embedded_python_runtime_windows_x86_64_manifest.json'
PACKAGE_JSON = ROOT / 'desktop-tauri' / 'package.json'
PACKAGE_LOCK = ROOT / 'desktop-tauri' / 'package-lock.json'
TAURI_CONFIG = ROOT / 'desktop-tauri' / 'src-tauri' / 'tauri.conf.json'
CARGO_MANIFEST = ROOT / 'desktop-tauri' / 'src-tauri' / 'Cargo.toml'
CARGO_LOCK = ROOT / 'desktop-tauri' / 'src-tauri' / 'Cargo.lock'
PROVENANCE = 'embedded_python_runtime_provenance.json'
FORBIDDEN = re.compile(r'(^|[\\/])(cmake|ninja|nvcc|cl|msbuild)(\.exe)?$|cuda[-_]?toolkit|visual studio|(^|[\\/])buildtools([\\/]|$)|\.sln$|\.vcxproj$', re.I)

class ValidationError(RuntimeError):
    pass



def _normalize_windows_version(value: str) -> str:
    parts = str(value or '').strip().split('.')
    if len(parts) not in {3, 4} or any(part == '' or not part.isdigit() for part in parts):
        raise ValidationError('Windows version metadata is missing or unparseable')
    if len(parts) == 4:
        if parts[3] != '0':
            raise ValidationError('Windows version metadata has unsupported fourth component')
        parts = parts[:3]
    return '.'.join(parts)


def _powershell_json(script: str, path: Path, *, description: str) -> dict:
    env = {**os.environ, 'TOKEN_PLACE_ARTIFACT_PATH': str(path)}
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command', script],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        raise ValidationError(f'{description} reader failed for {path.name}')
    text = (result.stdout or '').strip()
    if not text:
        raise ValidationError(f'{description} reader returned empty metadata for {path.name}')
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f'{description} reader returned malformed metadata for {path.name}') from exc
    if not isinstance(data, dict):
        raise ValidationError(f'{description} reader returned malformed metadata for {path.name}')
    return data


def _read_pe_version_info(path: Path) -> dict[str, str]:
    if platform.system() != 'Windows':
        return {}
    script = r'''
$ErrorActionPreference = 'Stop'
$p = $env:TOKEN_PLACE_ARTIFACT_PATH
if ([string]::IsNullOrWhiteSpace($p)) { throw 'missing artifact path' }
$v = (Get-Item -LiteralPath $p).VersionInfo
[PSCustomObject]@{ ProductVersion = [string]$v.ProductVersion; FileVersion = [string]$v.FileVersion } | ConvertTo-Json -Compress
'''
    data = _powershell_json(script, path, description='PE version')
    product = str(data.get('ProductVersion') or '').strip()
    file_version = str(data.get('FileVersion') or '').strip()
    if not product or not file_version:
        raise ValidationError(f'PE version reader returned incomplete metadata for {path.name}')
    return {'ProductVersion': product, 'FileVersion': file_version}


def _read_msi_product_version(path: Path) -> str | None:
    if platform.system() != 'Windows':
        return None
    script = r'''
$ErrorActionPreference = 'Stop'
$p = $env:TOKEN_PLACE_ARTIFACT_PATH
if ([string]::IsNullOrWhiteSpace($p)) { throw 'missing artifact path' }
$installer = New-Object -ComObject WindowsInstaller.Installer
$db = $installer.GetType().InvokeMember('OpenDatabase','InvokeMethod',$null,$installer,@($p,0))
$view = $db.OpenView("SELECT ``Value`` FROM ``Property`` WHERE ``Property``='ProductVersion'")
$view.Execute()
$record = $view.Fetch()
$value = if ($record) { [string]$record.StringData(1) } else { '' }
[PSCustomObject]@{ ProductVersion = $value } | ConvertTo-Json -Compress
'''
    data = _powershell_json(script, path, description='MSI ProductVersion')
    value = str(data.get('ProductVersion') or '').strip()
    if not value:
        raise ValidationError(f'MSI ProductVersion reader returned incomplete metadata for {path.name}')
    return value


def _expected_tauri_binary_name() -> str:
    tauri = _load_json(TAURI_CONFIG)
    product = str(tauri.get('bundle', {}).get('windows', {}).get('mainBinaryName') or '').strip()
    if not product:
        data = tomllib.loads(CARGO_MANIFEST.read_text(encoding='utf-8'))
        product = str(data.get('package', {}).get('name') or '').strip()
    if not product:
        raise ValidationError('unable to determine expected Tauri app executable name')
    return product if product.lower().endswith('.exe') else f'{product}.exe'


def _find_tauri_app_exe(root: Path) -> Path | None:
    expected = _expected_tauri_binary_name().lower()
    candidates = [p for p in root.rglob('*') if p.is_file() and p.suffix.lower() == '.exe' and p.name.lower() == expected]
    if len(candidates) != 1:
        raise ValidationError(f'expected exactly one installed app executable named {expected}, found {len(candidates)}')
    return candidates[0]


def _validate_version_metadata(artifact: Path, dest: Path, expected: str, kind: str) -> None:
    if artifact.is_dir():
        return
    values: dict[str, str | None] = {}
    if kind.upper() == 'MSI':
        values['MSI ProductVersion'] = _read_msi_product_version(artifact)
    elif artifact.is_file():
        nsis_versions = _read_pe_version_info(artifact)
        values['NSIS ProductVersion'] = nsis_versions.get('ProductVersion')
        values['NSIS FileVersion'] = nsis_versions.get('FileVersion')
    if platform.system() != 'Windows' and artifact.is_file():
        return
    app_exe = _find_tauri_app_exe(dest)
    app_versions = _read_pe_version_info(app_exe)
    values['app ProductVersion'] = app_versions.get('ProductVersion')
    values['app FileVersion'] = app_versions.get('FileVersion')
    failed = []
    for label, value in values.items():
        if not value:
            failed.append(f'{label}=missing')
            continue
        try:
            normalized = _normalize_windows_version(value)
        except ValidationError:
            failed.append(f'{label}=unparseable')
            continue
        if normalized != expected:
            failed.append(f'{label}={normalized}')
    if failed:
        raise ValidationError(f'{kind} version metadata mismatch for {artifact.name}: {failed}; expected {expected}')

def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def expected_version_from_tag(tag: str | None, fallback: str) -> str:
    if not tag:
        return fallback
    match = re.fullmatch(r'desktop-v(\d+\.\d+\.\d+)', tag)
    if not match:
        raise ValidationError('release tag must match desktop-vX.Y.Z')
    return match.group(1)


def _root_cargo_lock_version() -> str | None:
    data = tomllib.loads(CARGO_LOCK.read_text(encoding='utf-8'))
    root_name = str(tomllib.loads(CARGO_MANIFEST.read_text(encoding='utf-8')).get('package', {}).get('name') or '')
    for package in data.get('package', []):
        if package.get('name') == root_name and 'source' not in package:
            return package.get('version')
    return None


def validate_config_versions(expected: str) -> None:
    package = _load_json(PACKAGE_JSON)
    lock = _load_json(PACKAGE_LOCK)
    tauri = _load_json(TAURI_CONFIG)
    cargo = tomllib.loads(CARGO_MANIFEST.read_text(encoding='utf-8'))
    values = {
        'package.json': package.get('version'),
        'package-lock.json': lock.get('version'),
        'tauri.conf.json': tauri.get('version'),
        'src-tauri/Cargo.toml': cargo.get('package', {}).get('version'),
        'src-tauri/Cargo.lock': _root_cargo_lock_version(),
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
    if not artifact.is_dir():
        name = artifact.name
        if kind.upper() == 'MSI':
            pattern = rf'.*{re.escape(expected)}.*\.msi$'
        else:
            pattern = rf'.*{re.escape(expected)}.*setup.*\.exe$'
        if not re.fullmatch(pattern, name, re.IGNORECASE):
            raise ValidationError(f'{kind} filename does not match expected version {expected}: {artifact.name}')
    with tempfile.TemporaryDirectory(prefix=f'token-place-{kind}-') as td:
        dest = Path(td) / 'extract'
        dest.mkdir()
        cleanup = _run_extract(artifact, dest)
        try:
            runtime = _find_runtime(dest)
            _validate_runtime_tree(runtime, manifest)
            _validate_version_metadata(artifact, dest, expected, kind)
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
