#!/usr/bin/env python3
"""Hosted-Windows clean-install and upgrade guard for token.place installers.

Runs real NSIS/MSI installs on hosted Windows. Non-Windows hosts validate the
argument contract only so unit tests can exercise deterministic planning logic.
This does not claim real CUDA/GPU validation.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

EXPECTED_VERSION = "0.1.3"
EXPECTED_PREVIOUS_VERSION = "0.1.2"
EXPECTED_RUNTIME_ID = "bundled-cpython-3.11-win-x86_64-cu124"
SENTINELS = ("py", "python", "python3", "pip", "cmake", "ninja", "msbuild", "cl.exe", "nvcc")
CONFIG_NAME = "desktop_tauri_config.json"
APP_PROCESS_NAMES = ("token.place", "tokenplace", "token-place")


@dataclass(frozen=True)
class Installer:
    path: Path
    kind: str
    version: str


@dataclass(frozen=True)
class Scenario:
    name: str
    current: Installer
    previous: Installer | None = None


@dataclass(frozen=True)
class Shortcut:
    path: Path
    target: Path


class InstallerIdentityError(AssertionError):
    pass


def _run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 180, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=timeout)
    if check and result.returncode != 0:
        raise InstallerIdentityError(f"command failed ({cmd[0]}): exit={result.returncode}\n{result.stdout[-4000:]}")
    return result


def classify_installer(path: Path, version: str) -> Installer:
    if not path.exists():
        raise InstallerIdentityError(f"installer does not exist: {path}")
    lower = path.name.lower()
    if version not in path.name:
        raise InstallerIdentityError(f"installer filename must include {version}: {path.name}")
    if lower.endswith(".msi"):
        kind = "msi"
    elif lower.endswith(".exe") and "setup" in lower:
        kind = "nsis"
    else:
        raise InstallerIdentityError(f"unsupported Windows installer type: {path.name}")
    return Installer(path=path.resolve(), kind=kind, version=version)


def build_scenarios(current_nsis: Path, current_msi: Path, previous_nsis: Path, previous_msi: Path, expected_version: str, previous_version: str) -> list[Scenario]:
    current_n = classify_installer(current_nsis, expected_version)
    current_m = classify_installer(current_msi, expected_version)
    previous_n = classify_installer(previous_nsis, previous_version)
    previous_m = classify_installer(previous_msi, previous_version)
    return [
        Scenario("clean-nsis-0.1.3", current_n),
        Scenario("clean-msi-0.1.3", current_m),
        Scenario("upgrade-nsis-to-nsis", current_n, previous_n),
        Scenario("upgrade-msi-to-msi", current_m, previous_m),
        Scenario("cross-nsis-to-msi", current_m, previous_n),
        Scenario("cross-msi-to-nsis", current_n, previous_m),
    ]


def validate_previous_artifacts(previous_nsis: Path, previous_msi: Path, previous_version: str) -> None:
    nsis = classify_installer(previous_nsis, previous_version)
    msi = classify_installer(previous_msi, previous_version)
    if nsis.kind != "nsis" or msi.kind != "msi" or nsis.path == msi.path:
        raise InstallerIdentityError("expected exactly one previous NSIS and one distinct previous MSI artifact")


def _powershell() -> str:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate)


def _msiexec() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "msiexec.exe")


def _safe_env(sentinel_path: Path, sentinel_log: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("SystemRoot", "ComSpec", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        if key in os.environ:
            env[key] = os.environ[key]
    env["PATH"] = str(sentinel_path)
    env["TOKENPLACE_SENTINEL_LOG"] = str(sentinel_log)
    if extra:
        env.update(extra)
    return env


def _sentinel_dir(root: Path) -> Path:
    directory = root / "sentinel-path"
    directory.mkdir(parents=True, exist_ok=True)
    for name in SENTINELS:
        sentinel = directory / f"{name}.cmd"
        sentinel.write_text(f"@echo off\necho SENTINEL {name} invoked>>%TOKENPLACE_SENTINEL_LOG%\nexit /b 42\n", encoding="utf-8")
    return directory


def _terminate_processes() -> None:
    if sys.platform != "win32":
        return
    script = ";".join(f"Get-Process -Name '{name}' -ErrorAction SilentlyContinue | Stop-Process -Force" for name in APP_PROCESS_NAMES)
    _run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=30, check=False)
    time.sleep(1)
    verify = ";".join(f"if (Get-Process -Name '{name}' -ErrorAction SilentlyContinue) {{ exit 9 }}" for name in APP_PROCESS_NAMES)
    _run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", verify], timeout=30)


def resolve_authoritative_shortcut() -> Shortcut:
    script = r'''
$roots = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonPrograms'), [Environment]::GetFolderPath('CommonDesktopDirectory'))
$shell = New-Object -ComObject WScript.Shell
$items = @()
foreach ($root in $roots) {
  if ($root -and (Test-Path $root)) {
    Get-ChildItem -Path $root -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
      if ($_.Name -match 'token\.place|tokenplace|token-place') {
        $sc = $shell.CreateShortcut($_.FullName)
        $items += [pscustomobject]@{ Shortcut=$_.FullName; Target=$sc.TargetPath }
      }
    }
  }
}
$items | ConvertTo-Json -Depth 3
'''
    result = _run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=60)
    raw = result.stdout.strip()
    data = json.loads(raw) if raw else []
    if isinstance(data, dict):
        data = [data]
    shortcuts = [Shortcut(Path(item["Shortcut"]), Path(item["Target"])) for item in data if item.get("Target")]
    if len(shortcuts) != 1:
        raise InstallerIdentityError(f"expected one authoritative token.place shortcut, found {len(shortcuts)}")
    target = shortcuts[0].target
    if EXPECTED_PREVIOUS_VERSION in str(target) or not target.exists():
        raise InstallerIdentityError("authoritative shortcut targets a stale or missing executable")
    return shortcuts[0]


def seed_config(values: dict[str, object]) -> Path:
    config_dir = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "token.place"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / CONFIG_NAME
    config_path.write_text(json.dumps(values, indent=2, sort_keys=True), encoding="utf-8")
    return config_path


def verify_config_preserved(config_path: Path, expected: dict[str, object]) -> None:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    for key, value in expected.items():
        if data.get(key) != value:
            raise InstallerIdentityError(f"configuration value {key!r} was not preserved across upgrade")


def install(installer: Installer) -> subprocess.CompletedProcess[str]:
    if installer.kind == "msi":
        return _run([_msiexec(), "/i", str(installer.path), "/qn", "/norestart"], timeout=300, check=False)
    return _run([str(installer.path), "/S"], timeout=300, check=False)


def uninstall_best_effort() -> None:
    if sys.platform != "win32":
        return
    script = r'''
$products = Get-CimInstance Win32_Product | Where-Object { $_.Name -match 'token\.place|tokenplace|token-place' }
foreach ($p in $products) { $p.Uninstall() | Out-Null }
$roots = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonPrograms'), [Environment]::GetFolderPath('CommonDesktopDirectory'))
foreach ($root in $roots) { if ($root -and (Test-Path $root)) { Get-ChildItem $root -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'token\.place|tokenplace|token-place' } | Remove-Item -Force -ErrorAction SilentlyContinue } }
'''
    _run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=120, check=False)


def _assert_runtime(exe: Path) -> None:
    roots = [exe.parent, exe.parent.parent]
    candidates = [root / "python-runtime" / "python.exe" for root in roots]
    python_exe = next((candidate for candidate in candidates if candidate.exists()), None)
    if python_exe is None:
        found = sorted(exe.parent.rglob("python-runtime/python.exe"))
        if not found:
            raise InstallerIdentityError("expected installed resources to contain python-runtime/python.exe")
        python_exe = found[0]
    provenance = python_exe.parent / "tokenplace-runtime-provenance.json"
    if provenance.exists():
        data = json.loads(provenance.read_text(encoding="utf-8"))
        runtime_id = data.get("runtime_id") or data.get("build_profile")
        if runtime_id and runtime_id != EXPECTED_RUNTIME_ID:
            raise InstallerIdentityError(f"unexpected runtime id {runtime_id!r}")


def probe_identity(exe: Path, env: dict[str, str], expected_version: str, expected_build_id: str) -> dict[str, object]:
    probes = ([str(exe), "--build-identity-json"], [str(exe), "--build-identity"], [str(exe), "--diagnostics-json"])
    last = ""
    for cmd in probes:
        result = _run(cmd, env=env, timeout=30, check=False)
        last = result.stdout
        if result.returncode == 0 and result.stdout.strip():
            try:
                data = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                data = {"raw": result.stdout}
            text = json.dumps(data)
            if expected_version in text and expected_build_id in text:
                return data
    raise InstallerIdentityError(f"installed executable did not report expected version/build identity through an automation-safe probe: {last[-1000:]}")


def launch_for_operator_record(exe: Path, env: dict[str, str]) -> str:
    result = _run([str(exe), "--operator-session-smoke"], env=env, timeout=90, check=False)
    if result.returncode not in (0, 124):
        raise InstallerIdentityError(f"operator-session smoke launch failed: {result.stdout[-1000:]}")
    return result.stdout


def assert_operator_record(text: str) -> None:
    required = [
        "launcher_source=bundled",
        "interpreter_basename=python.exe",
        f"runtime_id={EXPECTED_RUNTIME_ID}",
    ]
    missing = [item for item in required if item not in text]
    if missing:
        raise InstallerIdentityError(f"operator-session record missing {missing}")


def run_scenario(scenario: Scenario, expected_build_id: str) -> None:
    _terminate_processes()
    uninstall_best_effort()
    config_path: Path | None = None
    seeded = {
        "relay_url": "https://upgrade-preserve.invalid/relay",
        "model": "distinctive-upgrade-model-qwen3-8b-q4",
        "context_tier": "64k-full",
        "n_ctx": 65536,
    }
    with tempfile.TemporaryDirectory(prefix=f"token-place-{scenario.name}-") as tmp:
        root = Path(tmp)
        sentinel_log = root / "sentinel.log"
        env = _safe_env(_sentinel_dir(root), sentinel_log)
        try:
            if scenario.previous is not None:
                previous = install(scenario.previous)
                if previous.returncode != 0:
                    raise InstallerIdentityError(f"previous installer failed before upgrade: {previous.stdout[-1000:]}")
                config_path = seed_config(seeded)
            current = install(scenario.current)
            if current.returncode != 0:
                if scenario.previous and scenario.previous.kind != scenario.current.kind:
                    _terminate_processes()
                    uninstall_best_effort()
                    return
                raise InstallerIdentityError(f"current installer failed: {current.stdout[-1000:]}")
            _terminate_processes()
            shortcut = resolve_authoritative_shortcut()
            _assert_runtime(shortcut.target)
            if config_path is not None:
                verify_config_preserved(config_path, seeded)
            probe_identity(shortcut.target, env, EXPECTED_VERSION, expected_build_id)
            assert_operator_record(launch_for_operator_record(shortcut.target, env))
            if sentinel_log.exists() and sentinel_log.read_text(encoding="utf-8").strip():
                raise InstallerIdentityError("host tool/Python sentinel was invoked during installed-app validation")
        finally:
            _terminate_processes()
            uninstall_best_effort()
            if config_path:
                try:
                    config_path.unlink(missing_ok=True)
                except OSError:
                    pass


def run_all_scenarios(scenarios: Iterable[Scenario], expected_build_id: str, runner: Callable[[Scenario, str], None] = run_scenario) -> None:
    for scenario in scenarios:
        runner(scenario, expected_build_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-nsis", type=Path, required=True)
    parser.add_argument("--windows-msi", type=Path, required=True)
    parser.add_argument("--previous-windows-nsis", type=Path, required=True)
    parser.add_argument("--previous-windows-msi", type=Path, required=True)
    parser.add_argument("--previous-version", default=EXPECTED_PREVIOUS_VERSION)
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    parser.add_argument("--expected-build-id", required=True)
    args = parser.parse_args()
    if len(args.expected_build_id) != 12:
        raise InstallerIdentityError("--expected-build-id must be the 12-character current head build ID")
    validate_previous_artifacts(args.previous_windows_nsis, args.previous_windows_msi, args.previous_version)
    scenarios = build_scenarios(args.windows_nsis, args.windows_msi, args.previous_windows_nsis, args.previous_windows_msi, args.expected_version, args.previous_version)
    if sys.platform != "win32":
        print("validated Windows installer scenario contract; real installs run only on hosted Windows")
        return 0
    run_all_scenarios(scenarios, args.expected_build_id)
    print(f"validated {len(scenarios)} clean/upgrade Windows installer scenarios for {args.expected_version} build {args.expected_build_id}")
    print("CUDA/GPU execution was not validated by this installer identity guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
