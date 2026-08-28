#!/usr/bin/env python3
"""Hosted-Windows clean-install and upgrade guard for token.place installers.

Runs real NSIS/MSI installs on hosted Windows. Non-Windows hosts validate the
argument contract only so unit tests can exercise deterministic planning logic.
This does not claim real CUDA/GPU validation.
"""
from __future__ import annotations

import argparse
import hashlib
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

EXPECTED_VERSION = "0.1.17"
EXPECTED_MODEL_ARTIFACT_FILENAME = "Qwen3-8B-Q4_K_M.gguf"
EXPECTED_RUNTIME_ID = "bundled-cpython-3.11-win-x86_64-cu124"
EXPECTED_TARGET_TRIPLE = "x86_64-pc-windows-msvc"
RUNTIME_PROVENANCE_NAME = "embedded_python_runtime_provenance.json"
OBSOLETE_RUNTIME_PROVENANCE_NAME = "tokenplace-runtime-" + "provenance.json"
SENTINELS = ("py", "python", "python3", "pip", "cmake", "ninja", "msbuild", "cl.exe", "nvcc")
CONFIG_NAME = "desktop_tauri_config.json"
TAURI_IDENTIFIER = "place.token.desktop"
APP_PROCESS_NAMES = ("token.place", "tokenplace", "token-place")
ACCEPTABLE_UNINSTALL_EXIT_CODES = frozenset({0, 1605, 1614, 3010})
WINDOWS_UNINSTALL_CLEANUP_TIMEOUT_SECONDS = 90.0
WINDOWS_UNINSTALL_REINVENTORY_PASSES = 3
HEADLESS_CPU_STARTUP_TIMEOUT_SECONDS = 300
HEADLESS_CPU_OPERATION_TIMEOUT_SECONDS = 600
HEADLESS_CPU_OUTER_ALLOWANCE_SECONDS = 30
HEADLESS_CPU_RESULT_KEYS = frozenset({
    "schema_version",
    "success",
    "last_completed_phase",
    "failure_code",
    "packaged_runtime_identity",
    "selected_backend",
    "warm_load_result",
    "authoritative_evidence_result",
})
HEADLESS_CPU_FAILURE_CODES = frozenset({
    "none",
    "command_not_first",
    "invalid_arguments",
    "unknown_argument",
    "duplicate_argument",
    "packaged_runtime_identity_failed",
    "unsupported_backend",
    "unsupported_context_tier",
    "invalid_timeout",
    "unusable_model_path",
    "installed_package_required",
    "mock_runtime_rejected",
    "bridge_exited_before_startup_event",
    "startup_timeout",
    "operation_timeout",
    "bridge_protocol_failed",
    "warm_load_failed",
    "authoritative_evidence_failed",
    "cleanup_failed",
})
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
EXPECTED_LLAMA_CPP_VERSION = "0.3.32"
EXPECTED_LLAMA_CPP_WHEEL = "llama_cpp_python-0.3.32-py3-none-win_amd64.whl"
EXPECTED_LLAMA_CPP_FLAVOR = "cu124"
EXPECTED_LLAMA_CPP_WHEEL_SHA256 = "c2149da0ff1af565418f27a9d11e88ed66732b3e2c46023e5d5dc0e30678fdc0"
EXPECTED_LLAMA_CPP_WHEEL_URL = "https://github.com/abetlen/llama-cpp-python/releases/download/v0.3.32-cu124/llama_cpp_python-0.3.32-py3-none-win_amd64.whl"
EXPECTED_CONTEXT_CAPABILITIES = {
    "8k-fast": {
        "api_v1_readiness_yarn_requested_context_tokens": 8192,
        "api_v1_readiness_yarn_original_context_tokens": 32768,
        "api_v1_readiness_yarn_context_multiplier": 1.0,
        "api_v1_readiness_yarn_rope_freq_scale": 1.0,
        "api_v1_readiness_yarn_ext_factor_overridden": False,
        "api_v1_readiness_yarn_rope_scaling_type_source": "not_required",
        "api_v1_readiness_yarn_rope_supported": True,
        "api_v1_readiness_yarn_rope_enabled": False,
        "api_v1_readiness_yarn_configuration_valid": True,
    },
    "64k-full": {
        "api_v1_readiness_yarn_requested_context_tokens": 65536,
        "api_v1_readiness_yarn_original_context_tokens": 32768,
        "api_v1_readiness_yarn_context_multiplier": 2.0,
        "api_v1_readiness_yarn_rope_freq_scale": 0.5,
        "api_v1_readiness_yarn_ext_factor_overridden": False,
        "api_v1_readiness_yarn_rope_scaling_type_source": "top_level_enum",
        "api_v1_readiness_yarn_rope_supported": True,
        "api_v1_readiness_yarn_rope_enabled": True,
        "api_v1_readiness_yarn_configuration_valid": True,
    },
}


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
    separate_stderr: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if separate_stderr else subprocess.STDOUT,
        env=env,
        timeout=timeout,
    )
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if separate_stderr:
            output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr or ''}"
        else:
            output = result.stdout
        log_path.write_text(f"$ {cmd[0]}\nexit={result.returncode}\n{output}", encoding="utf-8")
    if check and result.returncode != 0:
        diagnostic = result.stdout
        if separate_stderr and result.stderr:
            diagnostic = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise InstallerIdentityError(f"command failed ({cmd[0]}): exit={result.returncode}\n{diagnostic[-4000:]}")
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


def build_current_package_scenario(current_nsis: Path, expected_version: str) -> Scenario:
    """Build the single clean-NSIS scenario used by the hosted-Windows PR gate."""
    installer = classify_installer(current_nsis, expected_version)
    if installer.kind != "nsis":
        raise InstallerIdentityError("current-package PR validation requires an NSIS installer")
    return Scenario(f"pr-clean-current-nsis-{expected_version}", installer)


def validate_previous_artifacts(previous_nsis: Path, previous_msi: Path, previous_version: str) -> None:
    nsis = classify_installer(previous_nsis, previous_version)
    msi = classify_installer(previous_msi, previous_version)
    if nsis.kind != "nsis" or msi.kind != "msi" or nsis.path == msi.path:
        raise InstallerIdentityError("expected exactly one previous NSIS and one distinct previous MSI artifact")


def immediate_prior_version(version: str) -> str:
    """Return the immediate prior stable patch release for a semantic version string.

    For '0.1.3' this returns '0.1.2'; for a future '0.1.6' it returns '0.1.5'.
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


def _safe_env(
    sentinel_path: Path,
    sentinel_log: Path,
    extra: dict[str, str] | None = None,
    *,
    include_path: bool = True,
) -> dict[str, str]:
    env = {}
    for key in ("SystemRoot", "ComSpec", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        if key in os.environ:
            env[key] = os.environ[key]
    if include_path:
        env["PATH"] = str(sentinel_path)
    env["TOKENPLACE_SENTINEL_LOG"] = str(sentinel_log)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update({
        "PYTHONHOME": str(sentinel_path / "poison-pythonhome"),
        "PYTHONPATH": str(sentinel_path / "poison-pythonpath"),
        "PYTHONUSERBASE": str(sentinel_path / "poison-userbase"),
        "VIRTUAL_ENV": str(sentinel_path / "poison-venv"),
        "CONDA_PREFIX": str(sentinel_path / "poison-conda"),
        "PIP_INDEX_URL": "https://invalid.token.place.local/simple",
        "PIP_NO_INDEX": "1",
        "CMAKE_ARGS": "-DTOKEN_PLACE_SENTINEL=ON",
        "FORCE_CMAKE": "1",
        "TOKEN_PLACE_SIDECAR_PYTHON": str(sentinel_path / "python.exe"),
        "TOKEN_PLACE_PYTHON_IMPORT_ROOT": str(sentinel_path / "poison-import-root"),
        "PROCESSOR_ARCHITECTURE": "ARM64",
        "PROCESSOR_ARCHITEW6432": "x86",
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _uninstaller_identity(entry: RegistryEntry) -> str:
    value = entry.product_code or entry.key_path or entry.uninstall_string or entry.display_name
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:12]


def _uninstaller_kind(entry: RegistryEntry) -> str:
    command = (entry.quiet_uninstall_string or entry.uninstall_string).casefold()
    return "msi" if entry.windows_installer or "msiexec" in command else "nsis"


def _write_uninstall_log(path: Path, entry: RegistryEntry, invocation: list[str], result: subprocess.CompletedProcess[str]) -> None:
    """Write useful uninstall evidence without leaking machine-specific command paths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_invocation = [Path(invocation[0]).name, *invocation[1:]]
    path.write_text(
        f"kind={_uninstaller_kind(entry)}\nidentity={_uninstaller_identity(entry)}\n"
        f"invocation={json.dumps(safe_invocation)}\nexit={result.returncode}\noutput:\n{result.stdout}",
        encoding="utf-8",
    )


def _snapshot_payload(snapshot: AuthoritySnapshot) -> dict[str, object]:
    return {
        "shortcuts": [
            {"path": str(item.path), "target": str(item.target), "target_exists": item.target.exists()}
            for item in snapshot.shortcuts.shortcuts
        ],
        "registry": [
            {
                "kind": _uninstaller_kind(item),
                "identity": _uninstaller_identity(item),
                "display_name": item.display_name,
            }
            for item in snapshot.registry
        ],
    }


def _persist_snapshot(directory: Path | None, name: str, snapshot: AuthoritySnapshot) -> None:
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"authority-{name}.json").write_text(
            json.dumps(_snapshot_payload(snapshot), indent=2, sort_keys=True), encoding="utf-8"
        )


def uninstall_best_effort(log_path: Path | None = None) -> None:
    if sys.platform != "win32":
        return
    snapshot = capture_authority_snapshot()
    artifact_directory = log_path.parent if log_path is not None else None
    _persist_snapshot(artifact_directory, "before", snapshot)
    invoked: set[str] = set()
    invocation_number = 0
    for _pass in range(WINDOWS_UNINSTALL_REINVENTORY_PASSES):
        entries = sorted(inventory_registry_entries(), key=lambda item: (_uninstaller_kind(item), _uninstaller_identity(item)))
        pending = [entry for entry in entries if _uninstaller_identity(entry) not in invoked]
        if not pending:
            break
        for entry in pending:
            identity = _uninstaller_identity(entry)
            invoked.add(identity)
            invocation_number += 1
            invocation = build_uninstall_invocation(entry)
            result = _run(invocation, timeout=180, check=False)
            if artifact_directory is not None:
                invocation_log = artifact_directory / (
                    f"{log_path.stem}-invocation-{invocation_number:02d}-{_uninstaller_kind(entry)}-{identity}.log"
                )
                _write_uninstall_log(invocation_log, entry, invocation, result)
            if result.returncode not in ACCEPTABLE_UNINSTALL_EXIT_CODES:
                raise InstallerIdentityError(
                    f"uninstaller exit {result.returncode} for {entry.display_name!r}: {result.stdout[-1000:]}"
                )
    after = capture_authority_snapshot()
    _persist_snapshot(artifact_directory, "after", after)
    wait_for_cleanup_convergence(snapshot, artifact_directory=artifact_directory)


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
    deadline_seconds: float = WINDOWS_UNINSTALL_CLEANUP_TIMEOUT_SECONDS,
    poll_seconds: float = 0.5,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    artifact_directory: Path | None = None,
) -> None:
    started = monotonic()
    deadline = started + deadline_seconds
    last_categories: list[str] = []
    reported_milestones: set[int] = set()
    while True:
        last_categories = residual_authority_categories(before)
        if not last_categories:
            if artifact_directory is not None:
                _persist_snapshot(artifact_directory, "final", capture_authority_snapshot())
            print(f"cleanup converged after {monotonic() - started:.1f}s; residual authority: none")
            return
        now = monotonic()
        elapsed = now - started
        for milestone in (0, 20, 60, 90):
            if elapsed >= milestone and milestone not in reported_milestones:
                reported_milestones.add(milestone)
                print(f"cleanup elapsed {elapsed:.1f}s; residual authority: {', '.join(last_categories)}")
        if now >= deadline:
            final = capture_authority_snapshot()
            _persist_snapshot(artifact_directory, "final", final)
            before_paths = {_canonical_path(item.path) for item in before.shortcuts.shortcuts}
            shortcut_details = [
                f"path={item.path}; target={item.target}; target_exists={item.target.exists()}; "
                f"present_before_uninstall={_canonical_path(item.path) in before_paths}"
                for item in final.shortcuts.shortcuts
            ]
            detail = " | ".join(shortcut_details) if shortcut_details else "none"
            raise InstallerIdentityError(
                f"cleanup did not converge after {elapsed:.1f}s; residual authority: {', '.join(last_categories)}; "
                f"remaining shortcuts: {detail}"
            )
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


def _installed_python_executable(exe: Path) -> Path:
    roots = [exe.parent, exe.parent.parent]
    candidates = [root / "python-runtime" / "python.exe" for root in roots]
    python_exe = next((candidate for candidate in candidates if candidate.exists()), None)
    if python_exe is None:
        found = sorted(exe.parent.rglob("python-runtime/python.exe"))
        if not found:
            raise InstallerIdentityError("expected installed resources to contain python-runtime/python.exe")
        python_exe = found[0]
    return python_exe


def _assert_runtime(exe: Path) -> None:
    python_exe = _installed_python_executable(exe)
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
    if data.get("target_triple") != EXPECTED_TARGET_TRIPLE:
        raise InstallerIdentityError("installed runtime provenance has unexpected target architecture")
    wheel = data.get("llama_cpp_cuda_wheel")
    expected_wheel = {
        "name": EXPECTED_LLAMA_CPP_WHEEL,
        "version": EXPECTED_LLAMA_CPP_VERSION,
        "flavor": EXPECTED_LLAMA_CPP_FLAVOR,
        "sha256": EXPECTED_LLAMA_CPP_WHEEL_SHA256,
        "url": EXPECTED_LLAMA_CPP_WHEEL_URL,
    }
    if not isinstance(wheel, dict) or any(wheel.get(key) != value for key, value in expected_wheel.items()):
        raise InstallerIdentityError("installed runtime has unexpected llama-cpp-python wheel identity")
    required_packages = data.get("required_packages")
    if not isinstance(required_packages, dict) or required_packages.get("llama-cpp-python") != EXPECTED_LLAMA_CPP_VERSION:
        raise InstallerIdentityError("installed runtime has unexpected llama-cpp-python package version")
    closure = data.get("pe_dll_closure")
    if not isinstance(closure, list) or not closure:
        raise InstallerIdentityError("installed runtime native PE closure is missing")
    closure_names: set[str] = set()
    for entry in closure:
        if not isinstance(entry, dict) or set(("path", "machine", "sha256")) - set(entry):
            raise InstallerIdentityError("installed runtime native PE closure entry is incomplete")
        relative = Path(str(entry["path"]))
        if relative.is_absolute() or ".." in relative.parts or entry["machine"] != "IMAGE_FILE_MACHINE_AMD64":
            raise InstallerIdentityError("installed runtime native PE closure entry is invalid")
        native_file = (python_exe.parent / relative).resolve()
        closure_names.add(relative.name.casefold())
        try:
            native_file.relative_to(python_exe.parent.resolve())
        except ValueError as exc:
            raise InstallerIdentityError("installed runtime native PE closure escapes runtime") from exc
        if not native_file.is_file():
            raise InstallerIdentityError("installed runtime native PE closure file is missing")
        digest = _sha256_file(native_file)
        if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or digest != entry["sha256"]:
            raise InstallerIdentityError("installed runtime native PE closure hash mismatch")
    required_native = data.get("required_native_dlls")
    if not isinstance(required_native, list) or not required_native or any(
        not isinstance(name, str) or Path(name).name.casefold() not in closure_names for name in required_native
    ):
        raise InstallerIdentityError("installed runtime native PE closure is incomplete")
    probe = _run(
        [str(python_exe), "-I", "-c", "import importlib.metadata as m,importlib.util as u,json; s=u.find_spec('llama_cpp'); print(json.dumps({'version':m.version('llama-cpp-python'),'origin':s.origin if s else None}))"],
        env={key: os.environ[key] for key in ("SystemRoot", "TEMP", "TMP") if key in os.environ},
        timeout=30,
        check=False,
    )
    try:
        identity = json.loads(probe.stdout.strip()) if probe.returncode == 0 else None
    except json.JSONDecodeError:
        identity = None
    if not isinstance(identity, dict) or identity.get("version") != EXPECTED_LLAMA_CPP_VERSION:
        raise InstallerIdentityError("installed bundled interpreter reported unexpected llama-cpp-python version")
    origin = identity.get("origin")
    if not isinstance(origin, str):
        raise InstallerIdentityError("installed bundled interpreter did not resolve llama_cpp module")
    try:
        origin_path = Path(origin).resolve()
        origin_path.relative_to(python_exe.parent.resolve())
    except ValueError as exc:
        raise InstallerIdentityError("installed bundled interpreter resolved llama_cpp outside runtime") from exc
    if not origin_path.is_file():
        raise InstallerIdentityError("installed bundled interpreter llama_cpp module origin is missing")


def run_native_load_probe(exe: Path, model: Path, artifact_path: Path | None = None) -> dict[str, object]:
    python_exe = _installed_python_executable(exe)
    code = (
        "import json,re,sys\n"
        "phase='import'\n"
        "record={'schema_version':1,'phase':phase,'success':False,'exception_class':None}\n"
        "try:\n"
        " import llama_cpp\n"
        " phase='construct';record['phase']=phase\n"
        " llama=llama_cpp.Llama(model_path=sys.argv[1],n_ctx=512,n_gpu_layers=0,verbose=False)\n"
        " phase='close';record['phase']=phase\n"
        " llama.close()\n"
        " record['success']=True\n"
        "except BaseException as exc:\n"
        " name=type(exc).__name__\n"
        " record['exception_class']=name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}',name) else 'native_exception'\n"
        "print(json.dumps(record,separators=(',',':')))\n"
    )
    command = [str(python_exe), "-I", "-c", code, str(model)]
    env = {key: os.environ[key] for key in ("SystemRoot", "TEMP", "TMP") if key in os.environ}
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = _run(command, env=env, timeout=60, check=False, separate_stderr=True)
        try:
            child = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            child = None
        valid = (
            isinstance(child, dict)
            and set(child) == {"schema_version", "phase", "success", "exception_class"}
            and type(child["schema_version"]) is int
            and child["schema_version"] == 1
            and type(child["success"]) is bool
            and child["phase"] in {"import", "construct", "close"}
            and (child["exception_class"] is None or (
                isinstance(child["exception_class"], str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", child["exception_class"])
            ))
        )
        if valid and result.returncode == 0:
            record = {
                **child,
                "exit_code": result.returncode,
                "stderr_present": bool(result.stderr),
                "timed_out": False,
            }
        else:
            record = {
                "schema_version": 1,
                "phase": "launch",
                "success": False,
                "exception_class": "process_nonzero" if result.returncode else "malformed_output",
                "exit_code": result.returncode,
                "stderr_present": bool(result.stderr),
                "timed_out": False,
            }
    except subprocess.TimeoutExpired:
        record = {
            "schema_version": 1,
            "phase": "launch",
            "success": False,
            "exception_class": "timeout",
            "exit_code": None,
            "stderr_present": False,
            "timed_out": True,
        }
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    print(
        f"native_load_probe phase={record['phase']} success={str(record['success']).lower()} "
        f"category={record['exception_class'] or 'none'}",
        flush=True,
    )
    return record


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


def headless_cpu_admission_command(exe: Path, model: Path) -> list[str]:
    return [
        str(exe),
        "--headless-cpu-admission",
        "--model",
        str(model),
        "--backend",
        "cpu",
        "--context-tier",
        "8k-fast",
        "--startup-timeout-seconds",
        str(HEADLESS_CPU_STARTUP_TIMEOUT_SECONDS),
        "--operation-timeout-seconds",
        str(HEADLESS_CPU_OPERATION_TIMEOUT_SECONDS),
    ]


def validate_headless_cpu_admission_result(stdout: str, returncode: int) -> dict[str, object]:
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise InstallerIdentityError("installed headless CPU admission emitted malformed or extra stdout") from exc
    if not isinstance(result, dict) or set(result) != HEADLESS_CPU_RESULT_KEYS:
        raise InstallerIdentityError("installed headless CPU admission emitted an incomplete or unsupported schema")
    expected_types = {
        "schema_version": int,
        "success": bool,
        "last_completed_phase": str,
        "failure_code": str,
        "packaged_runtime_identity": str,
        "selected_backend": str,
        "warm_load_result": str,
        "authoritative_evidence_result": str,
    }
    if any(type(result[key]) is not value_type for key, value_type in expected_types.items()):
        raise InstallerIdentityError("installed headless CPU admission emitted invalid schema field types")
    success = (
        result["schema_version"] == 1
        and result["success"] is True
        and result["last_completed_phase"] == "cleanup_completed"
        and result["failure_code"] == "none"
        and result["packaged_runtime_identity"] == "validated"
        and result["selected_backend"] == "cpu"
        and result["warm_load_result"] == "ready"
        and result["authoritative_evidence_result"] == "validated"
    )
    if returncode != 0 or not success:
        raise InstallerIdentityError("installed headless CPU admission result did not agree with exit code 0")
    return result


def _privacy_safe_headless_terminal(stdout: str) -> dict[str, object] | None:
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict) or set(result) != HEADLESS_CPU_RESULT_KEYS:
        return None
    if (
        type(result["schema_version"]) is not int
        or type(result["success"]) is not bool
        or any(type(result[key]) is not str for key in HEADLESS_CPU_RESULT_KEYS - {"schema_version", "success"})
        or result["schema_version"] != 1
        or result["last_completed_phase"] not in {
            "not_started", "arguments_validated", "runtime_identity_validated", "warm_load_completed", "cleanup_completed"
        }
        or result["failure_code"] not in HEADLESS_CPU_FAILURE_CODES
        or result["packaged_runtime_identity"] not in {"failed", "validated"}
        or result["selected_backend"] != "cpu"
        or result["warm_load_result"] not in {"not_started", "ready"}
        or result["authoritative_evidence_result"] not in {"failed", "validated"}
    ):
        return None
    return result


def run_headless_cpu_admission(
    exe: Path,
    model: Path,
    env: dict[str, str],
    artifact_path: Path | None = None,
) -> dict[str, object]:
    command = headless_cpu_admission_command(exe, model)
    timeout = (
        HEADLESS_CPU_STARTUP_TIMEOUT_SECONDS
        + HEADLESS_CPU_OPERATION_TIMEOUT_SECONDS
        + HEADLESS_CPU_OUTER_ALLOWANCE_SECONDS
    )
    result: subprocess.CompletedProcess[str] | None = None
    terminal: dict[str, object] | None = None
    error: InstallerIdentityError | None = None
    try:
        result = _run(command, env=env, timeout=timeout, check=False, separate_stderr=True)
        terminal = _privacy_safe_headless_terminal(result.stdout)
        try:
            validate_headless_cpu_admission_result(result.stdout, result.returncode)
        except InstallerIdentityError as exc:
            error = exc
    except subprocess.TimeoutExpired:
        error = InstallerIdentityError("installed headless CPU admission exceeded its bounded outer timeout")
    if artifact_path is not None:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        evidence: dict[str, object] = {
            "command": "headless-cpu-admission",
            "exit_code": result.returncode if result is not None else None,
            "stderr_present": bool(result and result.stderr),
            "timed_out": result is None,
        }
        if terminal is not None:
            evidence["terminal_result"] = terminal
        artifact_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    # TEMPORARY bounded diagnostic for PR #1715 (warm_load_failed root cause
    # investigation). Reads the diag file back in this same Python process
    # (not a separate pwsh step reading a separately-derived path), using
    # the exact same `model` Path object passed into this call, to remove
    # any remaining doubt about cross-process/cross-step path derivation.
    # Prints only a phase tag and an exception class name, never file
    # contents beyond that. Removes the file after reading so it does not
    # leak into later scenario runs. Remove once root cause is confirmed.
    diag_path = model.parent / "tokenplace-headless-diag.txt"
    diag_found = False
    for _diag_attempt in range(10):
        if diag_path.is_file():
            print(f"headless_admission_diag_same_process_read={diag_path.read_text(encoding='utf-8', errors='ignore')!r} attempt={_diag_attempt}", flush=True)
            diag_path.unlink(missing_ok=True)
            diag_found = True
            break
        time.sleep(0.3)
    if not diag_found:
        try:
            listing = sorted(p.name for p in model.parent.iterdir())
        except OSError as exc:
            listing = [f"<listdir failed: {type(exc).__name__}>"]
        print(f"headless_admission_diag_same_process_read=<no diagnostic file written after retries> dir_listing={listing!r}", flush=True)
    if error is not None:
        raise error
    assert terminal is not None
    return terminal


def launch_for_operator_record(exe: Path, env: dict[str, str], log_path: Path | None = None) -> str:
    # Uses --operator-start-preflight-cpu-smoke, not --operator-start-preflight:
    # this script runs on hosted Windows runners with no NVIDIA GPU/driver
    # present (see module docstring), and the real preflight now performs
    # genuine CUDA validation (see PR #1549) rather than a fabricated ready
    # event, so it correctly requires a GPU to pass. The CPU-smoke variant
    # validates the same packaging/launch/resource-resolution structure
    # without claiming GPU/CUDA validation. Real GPU regression coverage is
    # tracked in https://github.com/futuroptimist/token.place/issues/1555
    # pending a self-hosted GPU runner.
    result = _run(
        [str(exe), "--operator-start-preflight-cpu-smoke"],
        env=env,
        timeout=90,
        check=False,
        log_path=log_path,
        separate_stderr=True,
    )
    if result.returncode not in (0, 124):
        diagnostic = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr or ''}"
        raise InstallerIdentityError(f"operator-session smoke launch failed: {diagnostic[-1000:]}")
    return result.stdout


def assert_operator_record(text: str, expected_tier: str | None = None, launch_number: int | None = None) -> dict[str, object]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise InstallerIdentityError("operator-session smoke must emit exactly one machine-parseable JSON record")
    try:
        data = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise InstallerIdentityError("operator-session smoke did not emit JSON") from exc
    expected = {
        "record": "desktop.compute_node.session.layout",
        "launcher_source": "bundled",
        "interpreter_basename": "python.exe",
        "runtime_id": EXPECTED_RUNTIME_ID,
        "bundled_runtime_id": EXPECTED_RUNTIME_ID,
    }
    missing = [key for key, value in expected.items() if data.get(key) != value]
    if missing:
        raise InstallerIdentityError(f"operator-session record missing or mismatched {missing}")
    if data.get("bridge_preflight") != "ok":
        raise InstallerIdentityError("operator-session smoke did not run bridge-command preflight")
    if data.get("model_artifact_inspect") != "ok":
        raise InstallerIdentityError("operator-session smoke did not run GUI-equivalent model artifact inspection")
    model_filename = data.get("model_artifact_filename")
    if (
        not isinstance(model_filename, str)
        or model_filename != EXPECTED_MODEL_ARTIFACT_FILENAME
        or not model_filename.endswith(".gguf")
        or "/" in model_filename
        or "\\" in model_filename
    ):
        raise InstallerIdentityError("operator-session smoke did not report the expected safe model artifact filename")
    if expected_tier is not None:
        expected_n_ctx = {"8k-fast": 8192, "64k-full": 65536}.get(expected_tier)
        if data.get("context_tier") != expected_tier or data.get("effective_n_ctx") != expected_n_ctx or data.get("n_ctx") != expected_n_ctx:
            raise InstallerIdentityError(f"operator-session smoke reported mismatched context tier/n_ctx for {expected_tier}")
        if data.get("selected_model_profile") != "qwen3-8b-q4":
            raise InstallerIdentityError("operator-session smoke did not select the Qwen3 8B Q4 profile")
        if data.get("startup_phase") == "provisioning" or data.get("startup_deadline_ms") is None:
            raise InstallerIdentityError("operator-session smoke did not report a bounded ready/terminal startup phase")
        if data.get("operator_start_preflight") == "ok":
            if data.get("resource_context_source") != "tauri_app_handle":
                raise InstallerIdentityError("operator-start preflight did not use the real Tauri AppHandle resource context")
            if data.get("bridge_child_spawned") is not True or data.get("bridge_event_received") is not True:
                raise InstallerIdentityError("operator-start preflight did not observe a spawned child and parsed bridge event")
            if data.get("native_runtime_validated") is not True or data.get("startup_result") != "runtime_validated":
                raise InstallerIdentityError("operator-start preflight did not validate the production native runtime; terminal_actionable_error is not success")
        elif data.get("operator_start_preflight") == "cpu_smoke_ok":
            # Structural-only variant (see launch_for_operator_record): deliberately
            # does not require native_runtime_validated/runtime_validated, since it
            # never claims GPU/CUDA validation in the first place.
            if data.get("resource_context_source") != "tauri_app_handle":
                raise InstallerIdentityError("operator-start preflight did not use the real Tauri AppHandle resource context")
            if data.get("bridge_child_spawned") is not True or data.get("bridge_event_received") is not True:
                raise InstallerIdentityError("operator-start preflight did not observe a spawned child and parsed bridge event")
            if data.get("selected_backend") != "cpu" or data.get("startup_result") != "cpu_smoke_validated":
                raise InstallerIdentityError("operator-start cpu-smoke preflight did not report a valid CPU-only structural result")
        elif data.get("startup_result") not in ("ready", "terminal_actionable_error"):
            raise InstallerIdentityError("operator-session smoke did not reach ready or a terminal actionable error")
        fallback_keys = ("fallback_reason", "backend_fallback", "model_fallback", "context_fallback")
        if data.get("fallback_reason") or any(data.get(key) is True for key in fallback_keys[1:]):
            raise InstallerIdentityError("operator-session smoke reported a fallback")
    assert_no_probe_attempt_counters(data)
    for key in (
        "provisioning_actions",
        "repair_actions",
        "pip_actions",
        "compiler_actions",
        "network_actions",
        "download_actions",
    ):
        if data.get(key, 0) not in (0, None):
            raise InstallerIdentityError(f"operator-start preflight reported forbidden action counter {key}")
    if launch_number == 2 and data.get("runtime_action") in {"installed_cuda_reexec", "installed_metal_reexec", "failed", "install_failed"}:
        raise InstallerIdentityError("second operator-session smoke launch reported runtime mutation action")
    if expected_tier is not None and launch_number is not None:
        capability_mismatches = [
            key for key, value in EXPECTED_CONTEXT_CAPABILITIES[expected_tier].items()
            if key not in data or data[key] != value
        ]
        if capability_mismatches:
            raise InstallerIdentityError(f"{expected_tier} smoke reported incomplete or mismatched YaRN/RoPE metadata: {capability_mismatches}")
    return data


def validate_installed_context_tiers(exe: Path, env: dict[str, str], artifact_dir: ScenarioArtifactDir | None, scenario_name: str) -> None:
    if "PATH" not in env:
        raise InstallerIdentityError("installed launch matrix is missing sentinel-only PATH coverage")
    if env.get("PATH") == os.environ.get("PATH"):
        raise InstallerIdentityError("installed launch matrix accidentally restored host PATH")
    if env.get("PROCESSOR_ARCHITECTURE") != "ARM64" or env.get("PROCESSOR_ARCHITEW6432") != "x86":
        raise InstallerIdentityError("installed launch matrix must poison both processor architecture variables")
    initial_manifest = capture_installed_resource_manifest(exe)
    expected_runtime_id: str | None = None
    expected_profile: str | None = None
    for tier in ("8k-fast", "64k-full"):
        config_path = seed_config(seeded_config_values(tier))
        for launch in (1, 2):
            before = capture_installed_resource_manifest(exe)
            assert_manifest_unchanged(initial_manifest, before, phase=f"{tier}-launch-{launch}-preflight")
            launch_env = dict(env)
            if launch == 1:
                launch_env.pop("PATH", None)
            launch_env["TOKENPLACE_INSTALLER_IDENTITY_LAUNCH_NUMBER"] = str(launch)
            text = launch_for_operator_record(
                exe,
                launch_env,
                artifact_dir.path(scenario_name, f"operator-smoke-{tier}-launch-{launch}") if artifact_dir else None,
            )
            after = capture_installed_resource_manifest(exe)
            assert_manifest_unchanged(before, after, phase=f"{tier}-launch-{launch}")
            record = assert_operator_record(text, expected_tier=tier, launch_number=launch)
            if record.get("target_triple") != EXPECTED_TARGET_TRIPLE:
                raise InstallerIdentityError("installed launcher did not report compiled Windows x86_64 attestation")
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


def run_scenario(
    scenario: Scenario,
    expected_build_id: str,
    artifact_dir: ScenarioArtifactDir | None = None,
    tokenizer_boundary_model: Path | None = None,
) -> None:
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
            if tokenizer_boundary_model is not None:
                run_native_load_probe(
                    shortcut.target,
                    tokenizer_boundary_model,
                    artifact_dir.path(scenario.name, "native-load-probe") if artifact_dir else None,
                )
                run_headless_cpu_admission(
                    shortcut.target,
                    tokenizer_boundary_model,
                    env,
                    artifact_dir.path(scenario.name, "headless-cpu-admission") if artifact_dir else None,
                )
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
    parser.add_argument(
        "--pr-current-windows-nsis",
        type=Path,
        default=None,
        help="Run the hosted-Windows PR gate against exactly one current-head NSIS package.",
    )
    parser.add_argument("--windows-nsis", type=Path)
    parser.add_argument("--windows-msi", type=Path)
    parser.add_argument("--previous-windows-nsis", type=Path)
    parser.add_argument("--previous-windows-msi", type=Path)
    parser.add_argument(
        "--previous-version",
        default=None,
        help="Immediate prior stable release version. Defaults to the semver-derived predecessor of --expected-version.",
    )
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    parser.add_argument("--expected-build-id", required=True)
    parser.add_argument("--artifact-dir", type=Path, default=None)
    parser.add_argument(
        "--tokenizer-boundary-model",
        type=Path,
        default=None,
        help="Tiny checksum-pinned GGUF required by the current-package headless CPU admission gate.",
    )
    args = parser.parse_args()
    if len(args.expected_build_id) != 12:
        raise InstallerIdentityError("--expected-build-id must be the 12-character current head build ID")
    if args.pr_current_windows_nsis is not None:
        if any((args.windows_nsis, args.windows_msi, args.previous_windows_nsis, args.previous_windows_msi, args.previous_version)):
            raise InstallerIdentityError("--pr-current-windows-nsis cannot be combined with full release scenario arguments")
        if args.tokenizer_boundary_model is None:
            raise InstallerIdentityError("--tokenizer-boundary-model is required with --pr-current-windows-nsis")
        if not args.tokenizer_boundary_model.is_file():
            raise InstallerIdentityError("--tokenizer-boundary-model must name an existing model file")
        scenario = build_current_package_scenario(args.pr_current_windows_nsis, args.expected_version)
        if sys.platform != "win32":
            print("validated current-package Windows NSIS PR-gate contract; real install runs only on hosted Windows")
            return 0
        artifacts = ScenarioArtifactDir(args.artifact_dir) if args.artifact_dir else None
        run_scenario(scenario, args.expected_build_id, artifacts, args.tokenizer_boundary_model.resolve())
        print(f"validated current-package Windows NSIS for {args.expected_version} build {args.expected_build_id}")
        return 0
    if args.tokenizer_boundary_model is not None:
        raise InstallerIdentityError("--tokenizer-boundary-model is only valid with --pr-current-windows-nsis")
    required = {
        "--windows-nsis": args.windows_nsis,
        "--windows-msi": args.windows_msi,
        "--previous-windows-nsis": args.previous_windows_nsis,
        "--previous-windows-msi": args.previous_windows_msi,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise InstallerIdentityError(f"full release validation requires {', '.join(missing)}")
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
