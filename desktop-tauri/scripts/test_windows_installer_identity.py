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
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

EXPECTED_VERSION = "0.1.5"
EXPECTED_MODEL_ARTIFACT_FILENAME = "Qwen3-8B-Q4_K_M.gguf"
EXPECTED_RUNTIME_ID = "bundled-cpython-3.11-win-x86_64-cu124"
RUNTIME_PROVENANCE_NAME = "embedded_python_runtime_provenance.json"
OBSOLETE_RUNTIME_PROVENANCE_NAME = "tokenplace-runtime-" + "provenance.json"
SENTINELS = ("py", "python", "python3", "pip", "cmake", "ninja", "msbuild", "cl.exe", "nvcc")
CONFIG_NAME = "desktop_tauri_config.json"
TAURI_IDENTIFIER = "place.token.desktop"
APP_PROCESS_NAMES = ("token.place", "tokenplace", "token-place")
ACCEPTABLE_UNINSTALL_EXIT_CODES = frozenset({0, 1605, 1614, 3010})
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


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
class ScenarioArtifactDir:
    root: Path

    def path(self, scenario: str, phase: str) -> Path:
        safe = scenario.replace("/", "-").replace("\\", "-")
        directory = self.root / safe
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{phase}.log"


@dataclass(frozen=True)
class Shortcut:
    path: Path
    target: Path


@dataclass(frozen=True)
class ShortcutInventory:
    shortcuts: list[Shortcut]
    existing_targets: list[Path]
    missing_targets: list[Path]

    @property
    def distinct_existing_targets(self) -> list[Path]:
        distinct: dict[str, Path] = {}
        for target in self.existing_targets:
            distinct[str(target).lower()] = target
        return list(distinct.values())


@dataclass(frozen=True)
class RegistryEntry:
    key_path: str
    display_name: str
    uninstall_string: str
    quiet_uninstall_string: str
    windows_installer: bool
    product_code: str


@dataclass(frozen=True)
class AuthoritySnapshot:
    shortcuts: ShortcutInventory
    registry: list[RegistryEntry]

    @property
    def canonical_targets(self) -> list[Path]:
        return self.shortcuts.distinct_existing_targets


@dataclass(frozen=True)
class InstalledResourceManifest:
    files: tuple[tuple[str, int, int], ...]

    def diff(self, other: "InstalledResourceManifest") -> dict[str, list[str]]:
        before = {path: (size, mtime_ns) for path, size, mtime_ns in self.files}
        after = {path: (size, mtime_ns) for path, size, mtime_ns in other.files}
        return {
            "added": sorted(set(after) - set(before)),
            "removed": sorted(set(before) - set(after)),
            "modified": sorted(path for path in set(before) & set(after) if before[path] != after[path]),
        }


class InstallerIdentityError(AssertionError):
    pass


def _run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 180,
    check: bool = True,
    log_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, timeout=timeout)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"$ {cmd[0]}\nexit={result.returncode}\n{result.stdout}", encoding="utf-8")
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
        Scenario(f"clean-nsis-{expected_version}", current_n),
        Scenario(f"clean-msi-{expected_version}", current_m),
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


def immediate_prior_version(version: str) -> str:
    """Return the immediate prior stable patch release for a semantic version string.

    For '0.1.3' this returns '0.1.2'; for a future '0.1.5' it returns '0.1.3'.
    """
    match = _SEMVER_RE.match(version)
    if not match:
        raise InstallerIdentityError(f"expected a semantic version X.Y.Z, got {version!r}")
    major, minor, patch = (int(part) for part in match.groups())
    if patch <= 0:
        raise InstallerIdentityError(
            f"version {version!r} has no immediate prior patch release; a non-patch predecessor "
            "must be selected explicitly via --previous-version"
        )
    return f"{major}.{minor}.{patch - 1}"


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
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    poison = str(sentinel_path / "poison")
    env.update({
        "PYTHONHOME": poison,
        "PYTHONPATH": poison,
        "PYTHONUSERBASE": poison,
        "VIRTUAL_ENV": poison,
        "CONDA_PREFIX": poison,
        "PIP_INDEX_URL": "https://example.invalid/simple",
        "PIP_REQUIRE_VIRTUALENV": "1",
        "CMAKE_ARGS": "-DGGML_CUDA=off",
        "FORCE_CMAKE": "1",
        "TOKEN_PLACE_PYTHON_IMPORT_ROOT": poison,
        "TOKEN_PLACE_SIDECAR_PYTHON": str(sentinel_path / "python.exe"),
        "TOKEN_PLACE_DESKTOP_PYTHON": str(sentinel_path / "python.exe"),
    })
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
    time.sleep(0.5)
    verify = ";".join(f"if (Get-Process -Name '{name}' -ErrorAction SilentlyContinue) {{ exit 9 }}" for name in APP_PROCESS_NAMES)
    _run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", verify], timeout=30)


def _canonical_path(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path).casefold()


def inventory_shortcuts() -> ShortcutInventory:
    script = r'''
$roots = @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonPrograms'), [Environment]::GetFolderPath('CommonDesktopDirectory'))
$shell = New-Object -ComObject WScript.Shell
$items = @()
foreach ($root in $roots) {
  if ($root -and (Test-Path $root)) {
    Get-ChildItem -Path $root -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
      if ($_.Name -match 'token\.place|tokenplace|token-place') {
        $sc = $shell.CreateShortcut($_.FullName)
        $target = $sc.TargetPath
        $exists = $false
        $resolved = $target
        if ($target -and (Test-Path -LiteralPath $target -PathType Leaf)) {
          $exists = $true
          $resolved = (Resolve-Path -LiteralPath $target).Path
        }
        $items += [pscustomobject]@{ Shortcut=$_.FullName; Target=$target; ResolvedTarget=$resolved; Exists=$exists }
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
    shortcuts: list[Shortcut] = []
    existing: list[Path] = []
    missing: list[Path] = []
    for item in data:
        if not item.get("Target"):
            continue
        target = Path(item.get("ResolvedTarget") or item["Target"])
        shortcut = Shortcut(Path(item["Shortcut"]), target)
        shortcuts.append(shortcut)
        exists = bool(item.get("Exists")) or target.exists()
        if exists:
            existing.append(target.resolve() if target.exists() else target)
        else:
            missing.append(target)
    return ShortcutInventory(shortcuts, existing, missing)


def inventory_registry_entries() -> list[RegistryEntry]:
    script = r'''
$roots = @("HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall")
$items = @()
foreach ($root in $roots) {
  if (Test-Path $root) {
    Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
      $p = Get-ItemProperty $_.PsPath -ErrorAction SilentlyContinue
      if ($p.DisplayName -match "token\.place|tokenplace|token-place") {
        $items += [pscustomobject]@{
          KeyPath = $_.PSPath
          DisplayName = $p.DisplayName
          UninstallString = $p.UninstallString
          QuietUninstallString = $p.QuietUninstallString
          WindowsInstaller = ($p.WindowsInstaller -eq 1)
          ProductCode = $p.PSChildName
        }
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
    entries: list[RegistryEntry] = []
    for item in data:
        display_name = item.get("DisplayName")
        if not display_name:
            continue
        entries.append(
            RegistryEntry(
                key_path=str(item.get("KeyPath") or ""),
                display_name=str(display_name),
                uninstall_string=str(item.get("UninstallString") or ""),
                quiet_uninstall_string=str(item.get("QuietUninstallString") or ""),
                windows_installer=bool(item.get("WindowsInstaller")),
                product_code=str(item.get("ProductCode") or ""),
            )
        )
    return entries


def capture_authority_snapshot() -> AuthoritySnapshot:
    return AuthoritySnapshot(shortcuts=inventory_shortcuts(), registry=inventory_registry_entries())


def _authority_signature(snapshot: AuthoritySnapshot) -> tuple:
    shortcuts = tuple(
        sorted(
            (
                _canonical_path(shortcut.path),
                _canonical_path(shortcut.target),
                shortcut.target.exists(),
            )
            for shortcut in snapshot.shortcuts.shortcuts
        )
    )
    missing = tuple(sorted(_canonical_path(target) for target in snapshot.shortcuts.missing_targets))
    targets = tuple(sorted(_canonical_path(target) for target in snapshot.canonical_targets))
    registry = tuple(
        sorted(
            (
                entry.key_path.casefold(),
                entry.display_name.casefold(),
                entry.uninstall_string.casefold(),
                entry.quiet_uninstall_string.casefold(),
                entry.windows_installer,
                entry.product_code.casefold(),
            )
            for entry in snapshot.registry
        )
    )
    return shortcuts, missing, targets, registry


def verify_authority_unchanged(before: AuthoritySnapshot, after: AuthoritySnapshot) -> None:
    if _authority_signature(before) != _authority_signature(after):
        raise InstallerIdentityError(
            "competing-installer rejection changed authority state; expected the existing "
            "installation's shortcut/executable/registry authority to remain exactly unchanged"
        )


def resolve_authoritative_shortcut(rejected_version: str | None = None) -> Shortcut:
    inventory = inventory_shortcuts()
    if not inventory.shortcuts:
        raise InstallerIdentityError("expected at least one authoritative token.place shortcut, found 0")
    if inventory.missing_targets:
        raise InstallerIdentityError("token.place shortcut inventory contains missing/stale executable targets")
    targets = inventory.distinct_existing_targets
    if not targets:
        raise InstallerIdentityError("token.place shortcut inventory contains zero existing executable targets")
    if len(targets) != 1:
        raise InstallerIdentityError(f"expected one distinct authoritative executable target, found {len(targets)}")
    target = targets[0]
    if rejected_version and rejected_version in str(target):
        raise InstallerIdentityError("authoritative shortcut targets a stale previous-version executable")
    return next(shortcut for shortcut in inventory.shortcuts if str(shortcut.target).lower() == str(target).lower())


def app_config_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / TAURI_IDENTIFIER


def seed_config(values: dict[str, object] | None = None) -> Path:
    config_dir = app_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / CONFIG_NAME
    payload = values or seeded_config_values()
    config_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return config_path


def seeded_config_values(context_tier: str = "64k-full") -> dict[str, object]:
    return {
        "relay_base_url": "https://upgrade-preserve-primary.invalid",
        "relay_base_urls": [
            "https://upgrade-preserve-primary.invalid",
            "https://upgrade-preserve-backup.invalid",
        ],
        "model_path": r"C:\\token-place-upgrade\\distinctive-qwen3-8b-q4.gguf",
        "preferred_mode": "gpu",
        "context_tier": context_tier,
    }


def verify_config_preserved(config_path: Path, expected: dict[str, object]) -> None:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    for key, value in expected.items():
        if data.get(key) != value:
            raise InstallerIdentityError(f"configuration value {key!r} was not preserved across upgrade")


def install(installer: Installer, log_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    if installer.kind == "msi":
        return _run([_msiexec(), "/i", str(installer.path), "/qn", "/norestart"], timeout=300, check=False, log_path=log_path)
    return _run([str(installer.path), "/S"], timeout=300, check=False, log_path=log_path)


def split_uninstall_command(command: str) -> tuple[str, str]:
    """Split a QuietUninstallString/UninstallString registry value into (executable, args).

    Handles the common Windows uninstall-string forms: a double-quoted executable
    path followed by arguments, or an unquoted executable path followed by
    whitespace-separated arguments.
    """
    command = command.strip()
    if not command:
        raise InstallerIdentityError("empty uninstall command")
    if command.startswith('"'):
        end = command.find('"', 1)
        if end < 0:
            raise InstallerIdentityError(f"unparsable quoted uninstall command: {command!r}")
        return command[1:end], command[end + 1 :].strip()
    parts = command.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def build_uninstall_invocation(entry: RegistryEntry) -> list[str]:
    """Return the argv to run for a silent uninstall of the given registry entry."""
    if entry.windows_installer and entry.product_code:
        return [_msiexec(), "/x", entry.product_code, "/qn", "/norestart"]
    command = entry.quiet_uninstall_string or entry.uninstall_string
    if not command:
        raise InstallerIdentityError(f"registry entry {entry.display_name!r} has no uninstall command")
    exe, raw_args = split_uninstall_command(command)
    args = raw_args.split() if raw_args else []
    lower_exe = exe.lower()
    if lower_exe.endswith("msiexec.exe") or lower_exe == "msiexec":
        if not any(arg.lower() in ("/x", "/uninstall") for arg in args):
            args = ["/x", *args]
        if not any(arg.lower() in ("/qn", "/quiet") for arg in args):
            args = [*args, "/qn", "/norestart"]
    else:
        if not any(arg.lower() in ("/s", "/quiet", "/qn") for arg in args):
            args = [*args, "/S"]
    return [exe, *args]


def uninstall_best_effort(log_path: Path | None = None) -> None:
    if sys.platform != "win32":
        return
    snapshot = capture_authority_snapshot()
    for entry in snapshot.registry:
        invocation = build_uninstall_invocation(entry)
        result = _run(invocation, timeout=180, check=False, log_path=log_path)
        if result.returncode not in ACCEPTABLE_UNINSTALL_EXIT_CODES:
            raise InstallerIdentityError(
                f"uninstaller exit {result.returncode} for {entry.display_name!r}: {result.stdout[-1000:]}"
            )
    wait_for_cleanup_convergence(snapshot)


def _parse_process_inventory(raw: str) -> list[dict[str, str]]:
    if not raw.strip():
        raise InstallerIdentityError("process inventory command emitted no JSON")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstallerIdentityError("process inventory command emitted invalid JSON") from exc
    if not isinstance(data, list):
        raise InstallerIdentityError("process inventory JSON must be an array")
    entries: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise InstallerIdentityError("process inventory entries must be objects")
        name = item.get("Name")
        executable_path = item.get("ExecutablePath")
        if not isinstance(name, str) or not isinstance(executable_path, str):
            raise InstallerIdentityError("process inventory entries must include string Name and ExecutablePath fields")
        entries.append({"Name": name, "ExecutablePath": executable_path})
    return entries


def _processes_running_targets(targets: Iterable[Path]) -> list[str]:
    wanted = {_canonical_path(target) for target in targets}
    if not wanted:
        return []
    script = r'''
$items = @(Get-CimInstance Win32_Process -ErrorAction Stop |
  Where-Object { $_.ExecutablePath } |
  Select-Object Name,ExecutablePath)
ConvertTo-Json -InputObject $items -Depth 3
'''
    result = _run([_powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], timeout=30, check=False)
    if result.returncode != 0:
        raise InstallerIdentityError("process inventory command failed")
    entries = _parse_process_inventory(result.stdout)
    return [
        entry["ExecutablePath"]
        for entry in entries
        if _canonical_path(Path(entry["ExecutablePath"])) in wanted
    ]


def _verify_no_authority_processes(targets: Iterable[Path]) -> None:
    running = _processes_running_targets(targets)
    if running:
        raise InstallerIdentityError(f"process authority remains after uninstall: {len(running)} process(es)")


def residual_authority_categories(before: AuthoritySnapshot | None = None) -> list[str]:
    categories: list[str] = []
    inventory = inventory_shortcuts()
    if inventory.shortcuts or inventory.existing_targets or inventory.missing_targets:
        categories.append("shortcuts")
    registry = inventory_registry_entries()
    if registry:
        categories.append("registry")
    targets = before.canonical_targets if before else []
    if any(target.exists() for target in targets):
        categories.append("executables")
    if _processes_running_targets(targets):
        categories.append("processes")
    return categories


def verify_no_authority_remains() -> None:
    categories = residual_authority_categories()
    if categories:
        raise InstallerIdentityError(f"authority remains: {', '.join(categories)}")


def verify_authority_removed(before: AuthoritySnapshot) -> None:
    categories = residual_authority_categories(before)
    if categories:
        raise InstallerIdentityError(f"authority remains after uninstall: {', '.join(categories)}")


def wait_for_cleanup_convergence(
    before: AuthoritySnapshot,
    *,
    deadline_seconds: float = 20.0,
    poll_seconds: float = 0.5,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    deadline = monotonic() + deadline_seconds
    last_categories: list[str] = []
    while True:
        last_categories = residual_authority_categories(before)
        if not last_categories:
            return
        if monotonic() >= deadline:
            raise InstallerIdentityError(f"cleanup did not converge; residual authority: {', '.join(last_categories)}")
        sleeper(poll_seconds)


def capture_installed_resource_manifest(exe: Path) -> InstalledResourceManifest:
    """Capture deterministic state for bundled runtime/resource roots only."""
    roots = [exe.parent / "python-runtime", exe.parent / "resources"]
    files: list[tuple[str, int, int]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            stat = path.stat()
            files.append((f"{root.name}/{path.relative_to(root).as_posix()}", int(stat.st_size), int(stat.st_mtime_ns)))
    return InstalledResourceManifest(tuple(files))


def assert_manifest_unchanged(before: InstalledResourceManifest, after: InstalledResourceManifest, *, phase: str) -> None:
    diff = before.diff(after)
    residual = {key: value[:5] for key, value in diff.items() if value}
    if residual:
        raise InstallerIdentityError(f"installed runtime/resource mutation after {phase}: {residual}")


def assert_no_probe_attempt_counters(data: dict[str, object]) -> None:
    counter_keys = (
        "runtime_installation_attempted_count",
        "runtime_repair_attempted_count",
        "dependency_provisioning_attempted_count",
        "provisioning_attempted_count",
        "network_attempted_count",
        "model_download_attempted_count",
    )
    nonzero = {key: data.get(key) for key in counter_keys if int(data.get(key) or 0) != 0}
    if nonzero:
        raise InstallerIdentityError(f"installed-context smoke attempted forbidden provisioning/network work: {nonzero}")


def _assert_runtime(exe: Path) -> None:
    roots = [exe.parent, exe.parent.parent]
    candidates = [root / "python-runtime" / "python.exe" for root in roots]
    python_exe = next((candidate for candidate in candidates if candidate.exists()), None)
    if python_exe is None:
        found = sorted(exe.parent.rglob("python-runtime/python.exe"))
        if not found:
            raise InstallerIdentityError("expected installed resources to contain python-runtime/python.exe")
        python_exe = found[0]
    provenance = python_exe.parent / RUNTIME_PROVENANCE_NAME
    if not provenance.exists():
        raise InstallerIdentityError(f"expected runtime provenance file at {provenance.name}, but it is missing")
    try:
        data = json.loads(provenance.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallerIdentityError(f"runtime provenance file is not valid JSON: {provenance.name}") from exc
    runtime_id = data.get("runtime_id") or data.get("build_profile")
    if runtime_id != EXPECTED_RUNTIME_ID:
        raise InstallerIdentityError(f"unexpected or missing runtime id in provenance: {runtime_id!r}")


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


def launch_for_operator_record(exe: Path, env: dict[str, str], log_path: Path | None = None) -> str:
    result = _run([str(exe), "--operator-start-preflight"], env=env, timeout=90, check=False, log_path=log_path)
    if result.returncode not in (0, 124):
        raise InstallerIdentityError(f"operator-start preflight launch failed: {result.stdout[-1000:]}")
    return result.stdout


def assert_operator_record(text: str, expected_tier: str | None = None, launch_number: int | None = None) -> dict[str, object]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise InstallerIdentityError("operator-start preflight must emit exactly one machine-parseable JSON record")
    try:
        data = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise InstallerIdentityError("operator-start preflight did not emit JSON") from exc
    expected = {
        "operator_start_preflight": "ok",
        "resource_context_source": "tauri_app_handle",
        "launcher_source": "bundled",
        "interpreter_basename": "python.exe",
        "runtime_id": EXPECTED_RUNTIME_ID,
        "bundled_runtime_id": EXPECTED_RUNTIME_ID,
    }
    missing = [key for key, value in expected.items() if data.get(key) != value]
    if missing:
        raise InstallerIdentityError(f"operator-start preflight record missing or mismatched {missing}")
    if data.get("bridge_preflight") != "ok":
        raise InstallerIdentityError("operator-start preflight did not run bridge-command preflight")
    for key, value in {"bridge_child_spawned": True, "bridge_event_received": True}.items():
        if data.get(key) != value:
            raise InstallerIdentityError(f"operator-start preflight missing controlled ready field {key}")
    if expected_tier is not None and data.get("startup_result") != "ready":
        raise InstallerIdentityError("operator-start preflight missing controlled ready field startup_result")
    if data.get("model_artifact_inspect") != "ok":
        raise InstallerIdentityError("operator-start preflight did not run GUI-equivalent model artifact inspection")
    model_filename = data.get("model_artifact_filename")
    if (
        not isinstance(model_filename, str)
        or model_filename != EXPECTED_MODEL_ARTIFACT_FILENAME
        or not model_filename.endswith(".gguf")
        or "/" in model_filename
        or "\\" in model_filename
    ):
        raise InstallerIdentityError("operator-start preflight did not report the expected safe model artifact filename")
    if expected_tier is not None:
        expected_n_ctx = {"8k-fast": 8192, "64k-full": 65536}.get(expected_tier)
        if data.get("context_tier") != expected_tier or data.get("effective_n_ctx") != expected_n_ctx or data.get("n_ctx") != expected_n_ctx:
            raise InstallerIdentityError(f"operator-start preflight reported mismatched context tier/n_ctx for {expected_tier}")
        if data.get("selected_model_profile") != "qwen3-8b-q4":
            raise InstallerIdentityError("operator-start preflight did not select the Qwen3 8B Q4 profile")
        if data.get("startup_phase") == "provisioning" or data.get("startup_deadline_ms") is None:
            raise InstallerIdentityError("operator-start preflight did not report a bounded ready/terminal startup phase")
        if data.get("startup_result") not in ("ready", "terminal_actionable_error"):
            raise InstallerIdentityError("operator-start preflight did not reach ready or a terminal actionable error")
        fallback_keys = ("fallback_reason", "backend_fallback", "model_fallback", "context_fallback")
        if data.get("fallback_reason") or any(data.get(key) is True for key in fallback_keys[1:]):
            raise InstallerIdentityError("operator-start preflight reported a fallback")
        if expected_tier == "64k-full":
            if data.get("api_v1_readiness_yarn_requested_context_tokens") != 65536 or data.get("api_v1_readiness_yarn_rope_supported") is not True:
                raise InstallerIdentityError("64k-full smoke did not satisfy fail-closed YaRN/RoPE capability contract")
    assert_no_probe_attempt_counters(data)
    if launch_number == 2 and data.get("runtime_action") in {"installed_cuda_reexec", "installed_metal_reexec", "failed", "install_failed"}:
        raise InstallerIdentityError("second operator-start preflight launch reported runtime mutation action")
    return data


def validate_installed_context_tiers(exe: Path, env: dict[str, str], artifact_dir: ScenarioArtifactDir | None, scenario_name: str) -> None:
    initial_manifest = capture_installed_resource_manifest(exe)
    expected_runtime_id: str | None = None
    expected_profile: str | None = None
    for tier in ("8k-fast", "64k-full"):
        config_path = seed_config(seeded_config_values(tier))
        for launch in (1, 2):
            before = capture_installed_resource_manifest(exe)
            assert_manifest_unchanged(initial_manifest, before, phase=f"{tier}-launch-{launch}-preflight")
            launch_env = dict(env)
            launch_env["TOKENPLACE_INSTALLER_IDENTITY_LAUNCH_NUMBER"] = str(launch)
            text = launch_for_operator_record(
                exe,
                launch_env,
                artifact_dir.path(scenario_name, f"operator-smoke-{tier}-launch-{launch}") if artifact_dir else None,
            )
            after = capture_installed_resource_manifest(exe)
            assert_manifest_unchanged(before, after, phase=f"{tier}-launch-{launch}")
            record = assert_operator_record(text, expected_tier=tier, launch_number=launch)
            runtime_id = str(record.get("runtime_id") or "")
            profile_id = str(record.get("model_profile_identifier") or record.get("active_model_profile_id") or "")
            if record.get("interpreter_basename") != "python.exe" or record.get("launcher_source") != "bundled" or runtime_id != EXPECTED_RUNTIME_ID:
                raise InstallerIdentityError("tier smoke did not use the installed bundled runtime")
            if expected_runtime_id is None:
                expected_runtime_id = runtime_id
            elif runtime_id != expected_runtime_id:
                raise InstallerIdentityError("tier smoke changed bundled runtime identity between launches")
            if expected_profile is None:
                expected_profile = profile_id
            elif profile_id != expected_profile:
                raise InstallerIdentityError("tier smoke changed canonical model profile between launches")
        verify_config_preserved(config_path, seeded_config_values(tier))


def is_actionable_competing_installer_rejection(result: subprocess.CompletedProcess[str]) -> bool:
    text = result.stdout.lower()
    return result.returncode != 0 and ("competing" in text or "existing installation" in text or "remove" in text) and ("token.place" in text or "token place" in text or "token-place" in text)


def run_scenario(scenario: Scenario, expected_build_id: str, artifact_dir: ScenarioArtifactDir | None = None) -> None:
    _terminate_processes()
    uninstall_best_effort()
    config_path: Path | None = None
    seeded = seeded_config_values()
    with tempfile.TemporaryDirectory(prefix=f"token-place-{scenario.name}-") as tmp:
        root = Path(tmp)
        sentinel_log = root / "sentinel.log"
        env = _safe_env(_sentinel_dir(root), sentinel_log)
        try:
            if scenario.previous is not None:
                previous = install(scenario.previous, artifact_dir.path(scenario.name, "install-previous") if artifact_dir else None)
                if previous.returncode != 0:
                    raise InstallerIdentityError(f"previous installer failed before upgrade: {previous.stdout[-1000:]}")
                config_path = seed_config(seeded)
            is_cross_kind = scenario.previous is not None and scenario.previous.kind != scenario.current.kind
            authority_before = capture_authority_snapshot() if is_cross_kind else None
            current = install(scenario.current, artifact_dir.path(scenario.name, "install-current") if artifact_dir else None)
            if current.returncode != 0:
                if is_cross_kind and is_actionable_competing_installer_rejection(current):
                    authority_after = capture_authority_snapshot()
                    verify_authority_unchanged(authority_before, authority_after)
                    _terminate_processes()
                    uninstall_best_effort(artifact_dir.path(scenario.name, "uninstall-after-rejection") if artifact_dir else None)
                    verify_no_authority_remains()
                    return
                raise InstallerIdentityError(f"current installer failed: {current.stdout[-1000:]}")
            _terminate_processes()
            shortcut = resolve_authoritative_shortcut(scenario.previous.version if scenario.previous else None)
            _assert_runtime(shortcut.target)
            probe_identity(shortcut.target, env, scenario.current.version, expected_build_id)
            record = assert_operator_record(launch_for_operator_record(shortcut.target, env, artifact_dir.path(scenario.name, "operator-smoke") if artifact_dir else None))
            if config_path is not None:
                verify_config_preserved(config_path, seeded)
                for key in ("context_tier", "preferred_mode"):
                    if str(record.get(key)) != str(seeded[key]):
                        raise InstallerIdentityError(f"operator smoke did not preserve seeded config field {key}")
            validate_installed_context_tiers(shortcut.target, env, artifact_dir, scenario.name)
            if sentinel_log.exists() and sentinel_log.read_text(encoding="utf-8").strip():
                raise InstallerIdentityError("host tool/Python sentinel was invoked during installed-app validation")
        finally:
            _terminate_processes()
            uninstall_best_effort(artifact_dir.path(scenario.name, "uninstall") if artifact_dir else None)
            if config_path:
                try:
                    config_path.unlink(missing_ok=True)
                except OSError:
                    pass


def run_all_scenarios(
    scenarios: Iterable[Scenario],
    expected_build_id: str,
    runner: Callable[..., None] = run_scenario,
    artifact_root: Path | None = None,
) -> None:
    artifacts = ScenarioArtifactDir(artifact_root) if artifact_root else None
    for scenario in scenarios:
        if runner is run_scenario:
            runner(scenario, expected_build_id, artifacts)
        else:
            runner(scenario, expected_build_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-nsis", type=Path, required=True)
    parser.add_argument("--windows-msi", type=Path, required=True)
    parser.add_argument("--previous-windows-nsis", type=Path, required=True)
    parser.add_argument("--previous-windows-msi", type=Path, required=True)
    parser.add_argument(
        "--previous-version",
        default=None,
        help="Immediate prior stable release version. Defaults to the semver-derived predecessor of --expected-version.",
    )
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    parser.add_argument("--expected-build-id", required=True)
    parser.add_argument("--artifact-dir", type=Path, default=None)
    args = parser.parse_args()
    if len(args.expected_build_id) != 12:
        raise InstallerIdentityError("--expected-build-id must be the 12-character current head build ID")
    previous_version = args.previous_version or immediate_prior_version(args.expected_version)
    validate_previous_artifacts(args.previous_windows_nsis, args.previous_windows_msi, previous_version)
    scenarios = build_scenarios(args.windows_nsis, args.windows_msi, args.previous_windows_nsis, args.previous_windows_msi, args.expected_version, previous_version)
    if sys.platform != "win32":
        print("validated Windows installer scenario contract; real installs run only on hosted Windows")
        return 0
    run_all_scenarios(scenarios, args.expected_build_id, artifact_root=args.artifact_dir)
    print(f"validated {len(scenarios)} clean/upgrade Windows installer scenarios for {args.expected_version} build {args.expected_build_id}")
    print("CUDA/GPU execution was not validated by this installer identity guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
