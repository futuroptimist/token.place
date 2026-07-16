#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
import stat
import time
from typing import NamedTuple
from pathlib import Path

STALE_BRANDS = ("tokenplace Desktop", "tokenplace Desktop Setup", "desktop/electron-builder")
DMG_PREVIEW_README_NAMES = ("README BEFORE OPENING.txt",)
DMG_PREVIEW_REQUIRED_PHRASES = (
    "not notarized",
    "Apple could not verify",
    "Privacy & Security",
    "Developer ID",
    "notarization",
)
DMG_PREVIEW_SIGNING_PHRASE_OPTIONS = (
    "ad-hoc signed",
    "signed with the configured Apple signing identity",
)


def _fail(msg: str) -> None:
    raise SystemExit(msg)


def _format_command_failure(cmd: list[str], result: subprocess.CompletedProcess[str]) -> str:
    return f"Command failed ({' '.join(cmd)}):\n{result.stdout}\n{result.stderr}"


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(_format_command_failure(cmd, result))
    return f"{result.stdout}\n{result.stderr}".strip()


def _run_with_retries(
    cmd: list[str],
    *,
    attempts: int,
    retry_messages: tuple[str, ...],
    delay_seconds: float = 2.0,
    max_delay_seconds: float = 15.0,
) -> str:
    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, attempts + 1):
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return f"{result.stdout}\n{result.stderr}".strip()

        last_result = result
        combined_output = f"{result.stdout}\n{result.stderr}".lower()
        should_retry = attempt < attempts and any(message.lower() in combined_output for message in retry_messages)
        if not should_retry:
            break

        retry_delay = min(delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
        print(
            f"::warning::Command {' '.join(cmd)} failed with a transient disk image error; "
            f"retrying attempt {attempt + 1}/{attempts} after {retry_delay:g}s."
        )
        time.sleep(retry_delay)

    if last_result is None:
        _fail(f"Command failed ({' '.join(cmd)}): no attempts were run")
    _fail(_format_command_failure(cmd, last_result))


def _run_best_effort(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"::warning::Best-effort command failed ({' '.join(cmd)}):\n"
            f"{result.stdout}\n{result.stderr}".strip()
        )
    return result


def _redact_hdiutil_info(info: str) -> str:
    home = str(Path.home())
    if home and home != "/":
        info = info.replace(home, "~")
    return re.sub(r"/private/var/folders/[^\s]+", "/private/var/folders/<redacted>", info)


def _hdiutil_info_raw() -> tuple[str, int]:
    result = subprocess.run(["hdiutil", "info"], check=False, capture_output=True, text=True)
    output = f"{result.stdout}\n{result.stderr}".strip()
    return output, result.returncode


def _hdiutil_info_snapshot(raw_info: str | None = None, returncode: int = 0) -> str:
    if raw_info is None:
        raw_info, returncode = _hdiutil_info_raw()
    if returncode != 0:
        return f"hdiutil info failed with exit code {returncode}:\n{_redact_hdiutil_info(raw_info)}"
    return _redact_hdiutil_info(raw_info)


def _hdiutil_info_plist() -> dict[str, object]:
    result = subprocess.run(["hdiutil", "info", "-plist"], check=False, capture_output=True)
    if result.returncode != 0:
        return {}
    try:
        parsed = plistlib.loads(result.stdout)
    except plistlib.InvalidFileException:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _image_entries(info_plist: dict[str, object]) -> list[dict[str, object]]:
    images = info_plist.get("images", [])
    if not isinstance(images, list):
        return []
    return [image for image in images if isinstance(image, dict)]


def _image_matches_dmg(image: dict[str, object], dmg_path: Path) -> bool:
    image_path = image.get("image-path")
    if not isinstance(image_path, str):
        return False
    return _path_matches_dmg(image_path, dmg_path)


def _path_matches_dmg(image_path: str, dmg_path: Path) -> bool:
    if image_path == str(dmg_path) or image_path == str(dmg_path.resolve()):
        return True
    try:
        if Path(image_path).resolve() == dmg_path.resolve():
            return True
    except OSError:
        pass
    return image_path.endswith(f"/{dmg_path}")


def _mountpoint_referenced(mount_dir: Path, info_plist: dict[str, object]) -> bool:
    mount = str(mount_dir)
    for image in _image_entries(info_plist):
        entities = image.get("system-entities", [])
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if isinstance(entity, dict) and entity.get("mount-point") == mount:
                return True
    return False


def _matching_hdiutil_devices_from_text(dmg_path: Path, raw_info: str) -> list[str]:
    devices: list[str] = []
    image_blocks = re.split(r"(?=^image-path\s*:)", raw_info, flags=re.MULTILINE)
    for block in image_blocks:
        image_match = re.search(r"^image-path\s*:\s*(.+)$", block, flags=re.MULTILINE)
        if image_match is None or not _path_matches_dmg(image_match.group(1).strip(), dmg_path):
            continue
        devices.extend(re.findall(r"^(/dev/disk\S+)", block, flags=re.MULTILINE))
    return devices


def _matching_hdiutil_devices(dmg_path: Path, info_plist: dict[str, object], raw_info: str = "") -> list[str]:
    devices: list[str] = []
    for image in _image_entries(info_plist):
        if not _image_matches_dmg(image, dmg_path):
            continue
        entities = image.get("system-entities", [])
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            device = entity.get("dev-entry")
            if isinstance(device, str) and device.startswith("/dev/disk"):
                devices.append(device)
    if raw_info:
        devices.extend(_matching_hdiutil_devices_from_text(dmg_path, raw_info))
    return list(dict.fromkeys(devices))


def _cleanup_dmg_attach_state(dmg_path: Path, mount_dir: Path) -> str:
    raw_info, raw_returncode = _hdiutil_info_raw()
    info_plist = _hdiutil_info_plist()
    if mount_dir.exists() and _mountpoint_referenced(mount_dir, info_plist):
        _run_best_effort(["hdiutil", "detach", str(mount_dir)])
        raw_info, raw_returncode = _hdiutil_info_raw()
        info_plist = _hdiutil_info_plist()
    for device in _matching_hdiutil_devices(dmg_path, info_plist, raw_info):
        _run_best_effort(["hdiutil", "detach", device])
    shutil.rmtree(mount_dir, ignore_errors=True)
    mount_dir.mkdir(parents=True, exist_ok=True)
    return _hdiutil_info_snapshot(raw_info, raw_returncode)


def _is_transient_hdiutil_attach_failure(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    transient_markers = (
        "resource temporarily unavailable",
        "resource busy",
        "device busy",
        "is busy",
        "already attached",
        "already mounted",
        "couldn't open helper",
        "diskimages-helper",
    )
    return any(marker in output for marker in transient_markers)


def _attach_dmg_with_retries(
    dmg_path: Path,
    *,
    attempts: int = 8,
    delay_seconds: float = 2.0,
    max_delay_seconds: float = 15.0,
) -> tempfile.TemporaryDirectory[str]:
    last_result: subprocess.CompletedProcess[str] | None = None
    mount_dirs: list[str] = []
    last_info = ""
    temp_dirs: list[tempfile.TemporaryDirectory[str]] = []

    for attempt in range(1, attempts + 1):
        temp_dir = tempfile.TemporaryDirectory(prefix="token-place-dmg-mount-")
        temp_dirs.append(temp_dir)
        mount_dir = Path(temp_dir.name)
        mount_dirs.append(str(mount_dir))
        last_info = _cleanup_dmg_attach_state(dmg_path, mount_dir)
        cmd = ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount_dir), str(dmg_path)]
        print(f"Attaching DMG {dmg_path} at {mount_dir} (attempt {attempt}/{attempts}).")
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            return temp_dir

        last_result = result
        if attempt >= attempts or not _is_transient_hdiutil_attach_failure(result):
            last_info = _cleanup_dmg_attach_state(dmg_path, mount_dir)
            break

        last_info = _cleanup_dmg_attach_state(dmg_path, mount_dir)
        retry_delay = min(delay_seconds * (2 ** (attempt - 1)), max_delay_seconds)
        print(
            f"::warning::hdiutil attach failed with a transient disk image error for {dmg_path} "
            f"at {mount_dir}; retrying attempt {attempt + 1}/{attempts} after {retry_delay:g}s.\n"
            f"stdout:\n{_redact_hdiutil_info(result.stdout)}\nstderr:\n{_redact_hdiutil_info(result.stderr)}".strip()
        )
        temp_dir.cleanup()
        temp_dirs.pop()
        time.sleep(retry_delay)

    for temp_dir in temp_dirs:
        temp_dir.cleanup()
    if last_result is None:
        _fail(f"hdiutil attach failed for {dmg_path}: no attempts were run")
    details = [
        f"hdiutil attach failed for DMG: {dmg_path}",
        f"mountpoints attempted: {mount_dirs}",
        f"attach attempts: {len(mount_dirs)}/{attempts}",
        f"last stdout:\n{_redact_hdiutil_info(last_result.stdout)}",
        f"last stderr:\n{_redact_hdiutil_info(last_result.stderr)}",
        f"redacted hdiutil info snapshot:\n{last_info}",
    ]
    _fail("\n".join(details))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()



def _redact_allowed_app_locations(output: str, app_path: Path) -> str:
    redacted = output

    # CI builds can run probes from an app bundle under host-specific absolute
    # paths (for example /Users/runner/...). Those app-local paths are expected
    # in sys.executable/sys.prefix output and should not trip the host-path leak
    # scan. Redact any absolute prefix ending at the same .app bundle name before
    # replacing relative app paths; otherwise a relative replacement can leave the
    # host-specific prefix visible (for example /Users/runner/.../<app-bundle>).
    app_name = re.escape(app_path.name)
    redacted = re.sub(rf"/[^\n\r]*?{app_name}", "<app-bundle>", redacted)

    allowed_paths = {str(app_path), str(app_path.absolute())}
    try:
        allowed_paths.add(str(app_path.resolve()))
    except OSError:
        pass
    try:
        allowed_paths.add(os.path.realpath(app_path))
    except OSError:
        pass
    for allowed in sorted(allowed_paths, key=len, reverse=True):
        if allowed:
            redacted = redacted.replace(allowed, "<app-bundle>")
    return redacted


def _run_python_sanitized(py: Path, code: str, app_path: Path) -> str:
    app_for_subprocess = app_path if app_path.is_absolute() else app_path.absolute()
    py_for_subprocess = py if py.is_absolute() else py.absolute()
    home = tempfile.mkdtemp(prefix="token-place-probe-")
    try:
        scratch = Path(home)
        app_data = scratch / "token.place"
        home_dir = scratch / "home"
        pycache = scratch / "pycache"
        tmpdir = scratch / "tmp"
        dependency_target = app_data / "desktop-site"
        for path in (app_data, dependency_target, home_dir, pycache, tmpdir):
            path.mkdir(parents=True, exist_ok=True)
        # env is a complete replacement for the subprocess environment. Host
        # interpreter overrides are intentionally not inherited by probes.
        env = {
            "HOME": str(home_dir),
            "TMPDIR": str(tmpdir),
            "PIP_CACHE_DIR": str(app_data / "pip-cache"),
            "PIP_NO_INDEX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPYCACHEPREFIX": str(pycache),
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(app_for_subprocess / "Contents" / "Resources" / "python"),
                    str(app_for_subprocess / "Contents" / "Resources"),
                ]
            ),
            "TOKEN_PLACE_DESKTOP_DEPENDENCY_TARGET": str(dependency_target),
            "TOKEN_PLACE_MODELS_DIR": str(app_data / "models"),
            "TOKEN_PLACE_MODEL_DIR": str(app_data / "models"),
            "TOKEN_PLACE_CACHE_DIR": str(app_data / "cache"),
            "HF_HOME": str(app_data / "hf-home"),
            "TRANSFORMERS_CACHE": str(app_data / "transformers-cache"),
            "XDG_CACHE_HOME": str(app_data / "cache"),
            "XDG_CONFIG_HOME": str(app_data / "config"),
            "XDG_DATA_HOME": str(app_data / "data"),
        }
        result = subprocess.run(
            [str(py_for_subprocess), "-B", "-c", code],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmpdir),
        )
        output = f"{result.stdout}\n{result.stderr}"
        scan_output = _redact_allowed_app_locations(output, app_for_subprocess)
        forbidden = ("/usr/bin/python3", "xcode-select", "No developer tools were found", "CommandLineTools", "/opt/homebrew", "/usr/local/Cellar", "pyenv", "/Users/runner", "/Library/Developer/CommandLineTools", "site.USER_SITE")
        for marker in forbidden:
            if marker in scan_output:
                _fail(f"embedded Python output leaked forbidden marker: {marker}")
        if result.returncode != 0:
            _fail(_format_command_failure([str(py_for_subprocess), "-B", "-c", "<probe>"], result))
        return output.strip()
    finally:
        shutil.rmtree(home, ignore_errors=True)

class AppTreeEntry(NamedTuple):
    kind: str
    sha256: str | None
    executable: bool
    symlink_target: str | None


def _app_tree_fingerprint(app_path: Path) -> dict[str, AppTreeEntry]:
    fingerprint: dict[str, AppTreeEntry] = {}
    for path in sorted(app_path.rglob("*")):
        rel = path.relative_to(app_path).as_posix()
        st = path.lstat()
        executable = bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
        if path.is_symlink():
            fingerprint[rel] = AppTreeEntry("symlink", None, executable, os.readlink(path))
        elif path.is_file():
            fingerprint[rel] = AppTreeEntry("file", _sha256(path), executable, None)
        elif path.is_dir():
            fingerprint[rel] = AppTreeEntry("dir", None, executable, None)
        else:
            fingerprint[rel] = AppTreeEntry("other", None, executable, None)
    return fingerprint


def _describe_app_tree_changes(before: dict[str, AppTreeEntry], after: dict[str, AppTreeEntry]) -> list[str]:
    changes: list[str] = []
    before_paths = set(before)
    after_paths = set(after)
    for rel in sorted(after_paths - before_paths):
        note = " (unsealed Python bytecode)" if rel.endswith(".pyc") or "/__pycache__/" in f"/{rel}" else ""
        changes.append(f"added: {rel}{note}")
    for rel in sorted(before_paths - after_paths):
        changes.append(f"removed: {rel}")
    for rel in sorted(before_paths & after_paths):
        old = before[rel]
        new = after[rel]
        if old.kind != new.kind:
            changes.append(f"type changed: {rel} ({old.kind} -> {new.kind})")
        elif old.sha256 != new.sha256:
            changes.append(f"rewritten: {rel}")
        elif old.executable != new.executable:
            changes.append(f"chmodded: {rel}")
        elif old.symlink_target != new.symlink_target:
            changes.append(f"retargeted symlink: {rel} ({old.symlink_target} -> {new.symlink_target})")
    return changes


def _run_with_app_mutation_guard(app_path: Path, action_name: str, action) -> None:
    before = _app_tree_fingerprint(app_path)
    action_error: BaseException | None = None
    try:
        action()
    except BaseException as exc:
        action_error = exc
    after = _app_tree_fingerprint(app_path)
    changes = _describe_app_tree_changes(before, after)
    if changes:
        sample = "\n".join(f"- {change}" for change in changes[:50])
        suffix = "" if len(changes) <= 50 else f"\n... {len(changes) - 50} more changes"
        _fail(f"{action_name} mutated app bundle {app_path}; executable probes must be non-mutating.\n{sample}{suffix}")
    if action_error is not None:
        raise action_error


def _codesign_verify(app_path: Path) -> None:
    if platform.system() == "Darwin":
        if shutil.which("codesign") is None:
            _fail("codesign not found in PATH on this macOS machine; cannot verify ad-hoc signature")
        _run(["codesign", "--verify", "--deep", "--strict", "--verbose=4", str(app_path)])


def _copy_app_for_runtime_validation(app_path: Path) -> tempfile.TemporaryDirectory[str]:
    temp_dir = tempfile.TemporaryDirectory(prefix="token-place-app-probe-")
    copy_path = Path(temp_dir.name) / app_path.name
    shutil.copytree(app_path, copy_path, symlinks=True)
    return temp_dir


def _validate_embedded_python_runtime_non_mutating(app_path: Path) -> None:
    _run_with_app_mutation_guard(app_path, "embedded Python runtime validation", lambda: _validate_embedded_python_runtime(app_path))

def _parse_otool_rpaths(load_commands: str) -> list[str]:
    rpaths: list[str] = []
    lines = load_commands.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "cmd LC_RPATH":
            for follow in lines[index + 1:index + 6]:
                stripped = follow.strip()
                if stripped.startswith("path "):
                    rpaths.append(stripped.split("path ", 1)[1].split(" (", 1)[0])
                    break
    return rpaths


def _parse_otool_install_ids(load_commands: str) -> list[str]:
    ids: list[str] = []
    lines = load_commands.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_ID_DYLIB":
            continue
        name: str | None = None
        next_command = len(lines)
        for cursor in range(index + 1, len(lines)):
            if lines[cursor].lstrip().startswith("Load command "):
                next_command = cursor
                break
        for follow in lines[index + 1:next_command]:
            stripped = follow.strip()
            if stripped.startswith("name "):
                value = stripped.removeprefix("name ")
                suffix = value.rfind(" (offset ")
                name = value[:suffix] if suffix != -1 else value
                break
        if name is None:
            _fail("malformed LC_ID_DYLIB load command without name")
        ids.append(name)
    if len(ids) > 1:
        _fail("multiple LC_ID_DYLIB load commands found")
    return ids


def _macho_archs(path: Path) -> list[str]:
    archs = _run(["lipo", "-archs", str(path)]).split()
    if not archs:
        _fail(f"Mach-O file has no architectures: {path}")
    if "arm64" not in archs:
        _fail(f"Mach-O file is not arm64: {path}")
    return archs


def _parse_otool_libraries(output: str, owner: Path, architecture: str | None) -> list[str]:
    owner_text = str(owner)
    expected_headers = {f"{owner_text}:"}
    if architecture is not None:
        expected_headers.add(f"{owner_text} (architecture {architecture}):")
    deps: list[str] = []
    saw_header = False
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.strip()
        is_header = stripped.endswith(":") and not raw_line[:1].isspace()
        if is_header:
            if stripped not in expected_headers:
                _fail(f"unexpected otool -L header for {owner.name}")
            if saw_header:
                _fail(f"duplicate otool -L header for {owner.name}")
            saw_header = True
            continue
        if not saw_header:
            _fail(f"dependency before otool -L header for {owner.name}")
        if not raw_line[:1].isspace():
            _fail(f"malformed otool -L record for {owner.name}")
        metadata = " (compatibility version "
        if metadata in stripped:
            stripped = stripped[:stripped.index(metadata)]
        if not stripped:
            _fail(f"malformed empty otool -L dependency for {owner.name}")
        deps.append(stripped)
    if not saw_header:
        _fail(f"missing otool -L header for {owner.name}")
    return deps


def _macho_file_kind(file_description: str) -> str:
    lower = file_description.lower()
    if "dynamically linked shared library" in lower or "dylib" in lower:
        return "dylib"
    if "bundle" in lower:
        return "bundle"
    if "executable" in lower:
        return "executable"
    return "other"


def _validate_macho_ref(ref: str, owner: Path, app_path: Path, *, install_id: bool = False, rpath: bool = False) -> None:
    if not ref:
        return
    if install_id and ref.startswith("/"):
        _fail(f"absolute Mach-O install ID in {owner}: {ref}")
    lower = ref.lower()
    forbidden = (
        "/opt/homebrew", "/usr/local/cellar", "pyenv", "commandlinetools",
        "/applications/xcode.app", "python.framework", "/users/runner",
        "/private/var/folders", "/" + "tmp/", "/var/" + "tmp/", "libssl.3.dylib", "libcrypto.3.dylib",
    )
    if any(marker in lower for marker in forbidden):
        _fail(f"forbidden external Mach-O linkage in {owner}: {ref}")
    if ref.startswith(("@loader_path", "@rpath", "@executable_path")):
        if rpath and not ref.startswith(("@loader_path", "@executable_path")):
            _fail(f"forbidden external Mach-O LC_RPATH in {owner}: {ref}")
        return
    if ref.startswith(("/usr/lib/", "/System/Library/")):
        return
    try:
        if Path(ref).resolve().is_relative_to(app_path.resolve()):
            return
    except OSError:
        pass
    if ref.startswith("/"):
        _fail(f"forbidden external Mach-O linkage in {owner}: {ref}")


def _safe_macho_ref(value: str) -> str:
    return Path(value).name or value[:80]

def _macho_relative(path: Path, app_path: Path) -> Path:
    try:
        return path.relative_to(app_path)
    except ValueError:
        return Path(path.name)

def _validate_macho_linkage(path: Path, app_path: Path) -> None:
    if platform.system() != "Darwin":
        return
    result = subprocess.run(["file", str(path)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(_format_command_failure(["file", str(path)], result))
    if "Mach-O" not in result.stdout:
        return
    file_description = result.stdout
    archs = _macho_archs(path)
    for arch in archs:
        deps = _parse_otool_libraries(_run(["otool", "-arch", arch, "-L", str(path)]), path, arch)
        for dep in deps:
            try:
                _validate_macho_ref(dep, path, app_path)
            except SystemExit as exc:
                rel = _macho_relative(path, app_path)
                _fail(f"native audit failed in {rel}: arch={arch} category=dependency ref={_safe_macho_ref(dep)}")
    load_commands_by_arch = {arch: _run(["otool", "-arch", arch, "-l", str(path)]) for arch in archs}
    install_ids_by_arch = {arch: _parse_otool_install_ids(output) for arch, output in load_commands_by_arch.items()}
    kind = _macho_file_kind(file_description)
    normalized_ids: list[str] = []
    for arch, install_ids in install_ids_by_arch.items():
        if kind == "dylib" and len(install_ids) != 1:
            _fail(f"Mach-O dylib is missing LC_ID_DYLIB: {path} [arch={arch} category=install_id]")
        for install_id in install_ids:
            try:
                _validate_macho_ref(install_id, path, app_path, install_id=True)
            except SystemExit as exc:
                rel = _macho_relative(path, app_path)
                _fail(f"native audit failed in {rel}: arch={arch} category=install_id ref={_safe_macho_ref(install_id)}")
            normalized_ids.append(install_id)
    if kind == "dylib" and len(set(normalized_ids)) != 1:
        _fail(f"Mach-O dylib install IDs differ by architecture: {path}")
    for arch, load_commands in load_commands_by_arch.items():
        for rpath in _parse_otool_rpaths(load_commands):
            try:
                _validate_macho_ref(rpath, path, app_path, rpath=True)
            except SystemExit as exc:
                rel = _macho_relative(path, app_path)
                _fail(f"native audit failed in {rel}: arch={arch} category=rpath ref={_safe_macho_ref(rpath)}")

def _validate_embedded_python_runtime(app_path: Path) -> None:
    runtime = app_path / "Contents" / "Resources" / "python-runtime"
    py = runtime / "bin" / "python3"
    if not py.exists() or not os.access(py, os.X_OK):
        _fail(f"embedded Python interpreter missing or not executable at exact packaged path: {py}")
    if not (runtime / "embedded_python_runtime_provenance.json").is_file():
        _fail("embedded runtime provenance missing")
    for notice in ("LICENSE-PYTHON.txt", "LICENSE-python-build-standalone.txt"):
        if not (runtime / notice).is_file():
            _fail(f"embedded runtime notice missing: {notice}")
    if platform.system() == "Darwin":
        arch = _run(["lipo", "-archs", str(py)]).lower()
        if "arm64" not in arch or ("x86_64" in arch and "arm64" not in arch):
            _fail(f"embedded Python is not arm64: {arch}")
    code = "import importlib.metadata as im,json,platform,sys; import psutil,requests,dotenv,cryptography,jinja2,numpy,diskcache,llama_cpp; print(json.dumps({'version':sys.version_info[:2],'machine':platform.machine(),'executable':sys.executable,'prefix':sys.prefix,'llama_cpp_python_version':im.version('llama-cpp-python')}))"
    payload = json.loads(_run_python_sanitized(py, code, app_path).splitlines()[-1])
    if payload.get("version") != [3, 11]:
        _fail(f"embedded Python is not CPython 3.11: {payload.get('version')}")
    if payload.get("machine") != "arm64":
        _fail(f"embedded Python is not arm64: {payload.get('machine')}")
    if payload.get("llama_cpp_python_version") != "0.3.32":
        _fail("embedded runtime has wrong llama-cpp-python version")
    for key in ("executable", "prefix"):
        if not Path(payload[key]).resolve().is_relative_to(app_path.resolve()):
            _fail(f"embedded Python {key} escaped app bundle: {payload[key]}")
    _run_python_sanitized(py, "import subprocess,sys; raise SystemExit(subprocess.run([sys.executable,'-m','pip','check']).returncode)", app_path)
    probe = "import json; from desktop_runtime_setup import _probe_llama_runtime; p=_probe_llama_runtime(); print(json.dumps(p.__dict__))"
    out = _run_python_sanitized(py, probe, app_path)
    data = json.loads(out.splitlines()[-1])
    if data.get("backend") != "metal" or not data.get("gpu_offload_supported"):
        _fail("embedded runtime probe did not report Metal GPU offload")
    if data.get("qwen_64k_yarn_support") != "supported":
        _fail("embedded runtime probe missing capability: qwen_64k_yarn_support")
    top_level_capabilities = {
        "rope_scaling_type": "rope_scaling_type_supported",
        "rope_freq_scale": "rope_freq_scale_supported",
        "yarn_orig_ctx": "yarn_orig_ctx_supported",
    }
    for name, field in top_level_capabilities.items():
        if not data.get(field):
            _fail(f"embedded runtime probe missing capability: {name}")
    constructor_support = data.get("constructor_kwarg_support") or {}
    for key in ("flash_attn", "offload_kqv", "n_batch", "n_ubatch"):
        if not constructor_support.get(key):
            _fail(f"embedded runtime probe missing capability: {key}")

    background_probe = r"""
import json, os, threading
import desktop_runtime_setup
from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(
    desktop_runtime_setup.__file__,
    avoid_llama_cpp_shadowing=True,
)

from desktop_runtime_setup import RUNTIME_PROBE_ENV, ensure_desktop_llama_runtime
from pathlib import Path
from utils.llm import model_manager

runtime_import_root = Path(model_manager.__file__).resolve().parents[2]
if not (runtime_import_root / 'requirements.txt').is_file():
    raise SystemExit('packaged runtime metadata missing: requirements.txt')

result = {}

def worker():
    setup = ensure_desktop_llama_runtime(
        'auto',
        repo_root=runtime_import_root,
        context_tier='64k-full',
    )
    private_runtime_setup = dict(setup)
    private_probe = json.loads(os.environ.get(RUNTIME_PROBE_ENV) or '{}')
    private_identity = private_probe.get('llama_module_identity')
    if isinstance(private_identity, str):
        private_runtime_setup['llama_module_identity'] = private_identity
    facade = model_manager._import_llama_cpp_runtime(
        require_real_runtime=True,
        timeout_seconds=10,
        desktop_runtime_probe=private_runtime_setup,
    )
    gate = model_manager._runtime_supports_qwen_yarn_rope(facade, facade.Llama)
    facade_capabilities = model_manager._safe_constructor_capability_payload(facade)
    constructor = gate.get('constructor_kwarg_support') or {}
    result.update({
        'runtime_action': setup.get('runtime_action'),
        'runtime_action_ok': setup.get('runtime_action') in {'metal_already_supported', 'already_supported'},
        'selected_backend': setup.get('selected_backend'),
        'llama_cpp_python_version_match': setup.get('llama_cpp_python_version_match'),
        'capability_source': gate.get('capability_source'),
        'incomplete_probe_fields': gate.get('incomplete_probe_fields'),
        'facade_type': type(facade).__name__,
        'backend': facade_capabilities.get('backend'),
        'gpu_offload_supported': facade_capabilities.get('gpu_offload_supported') is True,
        'version': gate.get('llama_cpp_python_version'),
        'yarn_resolver_source': gate.get('yarn_resolver_source'),
        'constructor_signature_inspectable': gate.get('constructor_signature_inspectable') is True,
        'required_kwargs_supported': all(constructor.get(name) for name in (
            'type_k', 'type_v', 'flash_attn', 'offload_kqv', 'n_batch', 'n_ubatch',
            'rope_scaling_type', 'yarn_ext_factor', 'yarn_attn_factor', 'yarn_beta_fast',
            'yarn_beta_slow', 'yarn_orig_ctx', 'rope_freq_base', 'rope_freq_scale')),
        'llama_module_identity_match': gate.get('llama_module_identity_match') is True,
        'supported': gate.get('supported') is True,
        'desktop_probe_authoritative': gate.get('desktop_probe_authoritative') is True,
        'secondary_reprobe_skipped': gate.get('child_probe_reprobe_skipped_reason') == 'desktop_probe_authoritative',
    })

thread = threading.Thread(target=worker, name='token-place-release-qwen64k-probe')
thread.start()
thread.join(timeout=30)
if thread.is_alive():
    raise SystemExit('background facade probe timed out')
print(json.dumps(result, sort_keys=True))
"""
    background_data = json.loads(_run_python_sanitized(py, background_probe, app_path).splitlines()[-1])
    expected_background = {
        "runtime_action_ok": True,
        "facade_type": "_SubprocessLlamaCppModule",
        "backend": "metal",
        "gpu_offload_supported": True,
        "version": "0.3.32",
        "yarn_resolver_source": "top_level_enum",
        "constructor_signature_inspectable": True,
        "required_kwargs_supported": True,
        "llama_module_identity_match": True,
        "supported": True,
        "desktop_probe_authoritative": True,
        "secondary_reprobe_skipped": True,
    }
    safe_background_diagnostics = {
        key: background_data.get(key)
        for key in ("runtime_action", "selected_backend", "llama_cpp_python_version_match", "capability_source", "llama_module_identity_match", "incomplete_probe_fields")
    }
    for key, expected in expected_background.items():
        if background_data.get(key) != expected:
            _fail(
                f"embedded background Qwen 64K facade probe failed {key}: "
                f"{background_data.get(key)!r}; diagnostics={safe_background_diagnostics!r}"
            )
    model_bridge = app_path / "Contents" / "Resources" / "python" / "model_bridge.py"
    if model_bridge.is_file():
        model_bridge_for_subprocess = model_bridge if model_bridge.is_absolute() else model_bridge.absolute()
        _run_python_sanitized(py, f"import subprocess,sys; raise SystemExit(subprocess.run([sys.executable, {str(model_bridge_for_subprocess)!r}, 'inspect']).returncode)", app_path)
    else:
        _fail("packaged model_bridge.py missing from app resources")
    for candidate in runtime.rglob("*"):
        if candidate.is_file(): _validate_macho_linkage(candidate, app_path)

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--app-path", required=True)
    p.add_argument("--dmg-path")
    p.add_argument("--app-only", action="store_true")
    p.add_argument("--tauri-config", required=True)
    p.add_argument("--expected-icon", required=True)
    p.add_argument("--expect-signing", action="store_true")
    p.add_argument("--require-embedded-python-runtime", action="store_true")
    p.add_argument("--expect-notarization", action="store_true")
    return p.parse_args()


def _validate_dmg_contents(dmg_path: Path, *, expect_signing: bool, require_embedded_python_runtime: bool = False) -> None:
    if platform.system() != "Darwin":
        print("::warning::Skipping DMG mounted-content checks outside macOS.")
        return
    mount_temp_dir = _attach_dmg_with_retries(dmg_path)
    mount_dir = mount_temp_dir.name
    try:
        try:
            root = Path(mount_dir)
            apps = sorted(p for p in root.iterdir() if p.is_dir() and p.suffix == ".app")
            if len(apps) != 1:
                _fail(f"DMG must contain exactly one .app at root; found {len(apps)}")
            readme_path = next((root / name for name in DMG_PREVIEW_README_NAMES if (root / name).is_file()), None)
            if readme_path is None:
                _fail(f"DMG must include one preview README at root: {DMG_PREVIEW_README_NAMES}")
            readme_text = readme_path.read_text(encoding="utf-8")
            missing = [phrase for phrase in DMG_PREVIEW_REQUIRED_PHRASES if phrase not in readme_text]
            if missing:
                _fail(f"DMG preview README missing required phrases: {missing}")
            if expect_signing:
                if DMG_PREVIEW_SIGNING_PHRASE_OPTIONS[1] not in readme_text:
                    _fail(
                        "DMG preview README must describe configured Apple signing identity when --expect-signing is set"
                    )
            elif DMG_PREVIEW_SIGNING_PHRASE_OPTIONS[0] not in readme_text:
                _fail("DMG preview README must include ad-hoc signing guidance for unsigned preview builds")
            mounted_app = apps[0]
            if require_embedded_python_runtime:
                _validate_embedded_python_runtime_non_mutating(mounted_app)
            _codesign_verify(mounted_app)
        finally:
            _cleanup_dmg_attach_state(dmg_path, Path(mount_dir))
    finally:
        mount_temp_dir.cleanup()


def main() -> None:
    args = _parse_args()
    app_path = Path(args.app_path)
    dmg_path = Path(args.dmg_path) if args.dmg_path else None
    tauri_config = Path(args.tauri_config)
    expected_icon = Path(args.expected_icon)

    dmg_candidates = (dmg_path.name, str(dmg_path)) if dmg_path is not None else ()
    for candidate in (app_path.name, *dmg_candidates):
        for stale in STALE_BRANDS:
            if stale.lower() in candidate.lower():
                _fail(f"stale Electron branding detected: {stale} in {candidate}")

    if not args.app_only:
        allow_macos_runtime_probe_without_dmg = (
            args.require_embedded_python_runtime and platform.system() == "Darwin" and dmg_path is None
        )
        if dmg_path is None and not allow_macos_runtime_probe_without_dmg:
            _fail("--dmg-path is required unless --app-only is set")
        if dmg_path is not None:
            if not dmg_path.exists() or dmg_path.suffix.lower() != '.dmg' or not dmg_path.is_file():
                _fail(f"dmg artifact missing or invalid: {dmg_path}")
            if not dmg_path.name.startswith("token.place-desktop-") or not dmg_path.name.endswith("-apple-silicon.dmg"):
                _fail(f"DMG filename must match token.place-desktop-<version>-apple-silicon.dmg: {dmg_path.name}")
            _validate_dmg_contents(dmg_path, expect_signing=args.expect_signing, require_embedded_python_runtime=args.require_embedded_python_runtime)

    if not app_path.exists() or app_path.suffix != ".app":
        _fail(f"app bundle missing or invalid: {app_path}")
    if not expected_icon.exists() or not expected_icon.is_file():
        _fail(f"expected icon missing or invalid: {expected_icon}")

    info_plist = app_path / "Contents" / "Info.plist"
    if not info_plist.exists():
        _fail(f"missing Info.plist: {info_plist}")
    info = plistlib.loads(info_plist.read_bytes())

    product_name = info.get("CFBundleName") or ""
    display_name = info.get("CFBundleDisplayName") or ""
    if "tokenplace desktop" in str(product_name).lower():
        _fail("stale app bundle name tokenplace Desktop detected in CFBundleName")
    if "tokenplace desktop" in str(display_name).lower():
        _fail("stale app display name tokenplace Desktop detected in CFBundleDisplayName")

    config = json.loads(tauri_config.read_text(encoding="utf-8"))
    icons = config.get("bundle", {}).get("icon", [])
    required_icons = {"icons/icon.icns", "icons/icon.ico", "icons/128x128@2x.png"}
    missing = sorted(required_icons - set(icons))
    if missing:
        _fail(f"tauri icon list missing required entries: {missing}")

    icon_key = info.get("CFBundleIconFile", "icon.icns")
    if not str(icon_key).endswith(".icns"):
        icon_key = f"{icon_key}.icns"
    bundled_icon = app_path / "Contents" / "Resources" / icon_key
    if not bundled_icon.exists():
        _fail(f"bundled icon not found: {bundled_icon}")
    if _sha256(bundled_icon) != _sha256(expected_icon):
        _fail("bundled icon hash does not match desktop-tauri/src-tauri/icons/icon.icns")

    macos_dir = app_path / "Contents" / "MacOS"
    if not macos_dir.exists() or not macos_dir.is_dir():
        _fail(f"missing app executable directory: {macos_dir}")
    executable_name = info.get("CFBundleExecutable")
    if not executable_name:
        _fail("CFBundleExecutable is missing from Info.plist")
    executable_path = macos_dir / str(executable_name)
    if not executable_path.exists() or not executable_path.is_file():
        _fail(f"CFBundleExecutable not found in app bundle: {executable_path}")
    arch_out = _run(["lipo", "-archs", str(executable_path)])
    arch_lower = arch_out.lower()
    if "arm64" not in arch_lower and "aarch64" not in arch_lower:
        _fail(f"binary is not Apple Silicon: {arch_out}")
    if "x86_64" in arch_lower and "arm64" not in arch_lower:
        _fail(f"binary is x86_64-only: {arch_out}")

    _codesign_verify(app_path)

    if args.require_embedded_python_runtime:
        should_probe_source_app_copy = args.app_only or platform.system() != "Darwin" or dmg_path is None
        if should_probe_source_app_copy:
            with _copy_app_for_runtime_validation(app_path) as probe_dir:
                probe_app = Path(probe_dir) / app_path.name
                _validate_embedded_python_runtime_non_mutating(probe_app)
                if platform.system() == "Darwin":
                    _codesign_verify(probe_app)

    _codesign_verify(app_path)

    if args.expect_notarization:
        _run(["spctl", "-a", "-vv", "--type", "execute", str(app_path)])
    elif args.expect_signing:
        print("::warning::Signing configured without notarization credentials; skipping strict Gatekeeper assessment.")
    else:
        print("::warning::Apple Developer signing credentials not configured; verifying the existing ad-hoc signature only.")


if __name__ == "__main__":
    main()
