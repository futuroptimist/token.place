#!/usr/bin/env python3
"""Prepare the relocatable Apple Silicon CPython runtime bundled in the .app."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = ROOT / "src-tauri"
MANIFEST = SRC_TAURI / "python" / "embedded_python_runtime_manifest.json"
OUTPUT = SRC_TAURI / "python-runtime"
PROVENANCE = "embedded_python_runtime_provenance.json"
BUILD_PROFILE = "metal-relocatable-no-openssl-libpython-rpath-v2"
IMPORTS = ["psutil", "requests", "dotenv", "cryptography", "jinja2", "numpy", "diskcache", "llama_cpp"]
if str(SRC_TAURI / "python") not in sys.path:
    sys.path.insert(0, str(SRC_TAURI / "python"))
from desktop_gpu_packaging import llama_cpp_install_plan_fallbacks  # noqa: E402

class RuntimePrepError(RuntimeError): pass

def load_manifest(path: Path = MANIFEST) -> dict:
    m = json.loads(path.read_text(encoding="utf-8"))
    if m.get("schema_version") != 1: raise RuntimePrepError("unsupported embedded runtime manifest schema_version")
    if not str(m.get("archive_url", "")).startswith("https://"): raise RuntimePrepError("archive_url must be HTTPS")
    sha = str(m.get("sha256", ""))
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha): raise RuntimePrepError("sha256 must be a lowercase 64-character hex digest")
    if m.get("target_triple") != "aarch64-apple-darwin": raise RuntimePrepError("manifest target_triple must be aarch64-apple-darwin")
    if m.get("expected_packaged_runtime_path") != "Contents/Resources/python-runtime/bin/python3": raise RuntimePrepError("unexpected packaged runtime path")
    for key in ["cpython_version","python_build_standalone_release","python_build_standalone_build","expected_archive_root","expected_interpreter_path","expected_architecture","minimum_macos_version","required_packages","runtime_notices"]:
        if key not in m: raise RuntimePrepError(f"missing manifest field: {key}")
    if "latest" in str(m["archive_url"]).lower(): raise RuntimePrepError("archive_url must not use latest")
    expected_packages = {
        "psutil": "7.1.0",
        "requests": "2.32.5",
        "python-dotenv": "1.1.1",
        "cryptography": "46.0.1",
        "Jinja2": "3.1.6",
        "numpy": "2.3.3",
        "diskcache": "5.6.3",
        "llama-cpp-python": "0.3.32",
    }
    if m.get("required_packages") != expected_packages: raise RuntimePrepError("required_packages must match exact embedded runtime package map")
    return m

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024*1024), b""): h.update(b)
    return h.hexdigest()

def download_verified(m: dict, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / Path(urllib.parse.urlparse(m["archive_url"]).path).name.replace("%2B", "+")
    if archive.exists() and sha256(archive) == m["sha256"]: return archive
    if archive.exists(): archive.unlink()
    tmp = archive.with_suffix(archive.suffix + ".tmp")
    urllib.request.urlretrieve(m["archive_url"], tmp)  # nosec B310 - manifest validation requires HTTPS and a pinned SHA-256 before extraction
    digest = sha256(tmp)
    if digest != m["sha256"]:
        tmp.unlink(missing_ok=True); raise RuntimePrepError(f"digest mismatch for embedded runtime archive: expected {m['sha256']} got {digest}")
    tmp.replace(archive)
    return archive

def validate_tar_member(member: tarfile.TarInfo, root: str) -> None:
    name = PurePosixPath(member.name)
    if name.is_absolute() or ".." in name.parts: raise RuntimePrepError(f"unsafe archive path: {member.name}")
    if not name.parts or name.parts[0] != root: raise RuntimePrepError(f"unexpected archive root: {member.name}")
    if member.islnk() or member.issym():
        target = PurePosixPath(member.linkname)
        if target.is_absolute() or ".." in target.parts: raise RuntimePrepError(f"archive link escapes extraction root: {member.name}")

def extract_archive(archive: Path, m: dict, tmp_parent: Path) -> Path:
    extract_dir = tmp_parent / "extract"; extract_dir.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        if not members: raise RuntimePrepError("runtime archive is empty")
        for member in members: validate_tar_member(member, m["expected_archive_root"])
        tf.extractall(extract_dir, filter="data")  # nosec B202 - all members are validated before extraction
    runtime = extract_dir / m["expected_archive_root"]
    if not (runtime / m["expected_interpreter_path"]).is_file(): raise RuntimePrepError("archive missing expected interpreter")
    return runtime

def run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy(); env.update(kw.pop("env", {}) or {})
    env["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(cmd, text=True, capture_output=True, check=False, env=env, **kw)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result

def prove_interpreter(py: Path, runtime: Path, m: dict) -> None:
    code = "import json,platform,sys; print(json.dumps({'version':sys.version_info[:2],'machine':platform.machine(),'executable':sys.executable,'prefix':sys.prefix}))"
    data = json.loads(run([str(py), "-c", code]).stdout)
    if data["version"] != [3, 11]: raise RuntimePrepError("bundled interpreter is not Python 3.11")
    if data["machine"] != m["expected_architecture"]: raise RuntimePrepError("bundled interpreter has wrong architecture")
    for key in ["executable", "prefix"]:
        if not Path(data[key]).resolve().is_relative_to(runtime.resolve()): raise RuntimePrepError(f"{key} is outside generated runtime")


def _python_major_minor(m: dict) -> tuple[int, int]:
    parts = str(m["cpython_version"]).split(".")
    if len(parts) < 2:
        raise RuntimePrepError("manifest cpython_version must include major.minor")
    return int(parts[0]), int(parts[1])

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
            raise RuntimePrepError("malformed LC_ID_DYLIB load command without name")
        ids.append(name)
    if len(ids) > 1:
        raise RuntimePrepError("multiple LC_ID_DYLIB load commands found")
    return ids

def _otool_install_id(path: Path) -> str:
    ids = _parse_otool_install_ids(_otool(["otool", "-l", str(path)]))
    if len(ids) != 1:
        raise RuntimePrepError(f"expected exactly one libpython install ID in {path}")
    return ids[0]

def _otool_load_deps(path: Path) -> list[str]:
    deps: list[str] = []
    for line in _otool(["otool", "-L", str(path)]).splitlines()[1:]:
        dep = line.strip().split(" ", 1)[0]
        if dep:
            deps.append(dep)
    return deps

def _install_name_tool(args: list[str]) -> None:
    run(["install_name_tool", *args])

def _ensure_owner_write(path: Path) -> None:
    mode = path.stat().st_mode
    if not mode & 0o200:
        path.chmod(mode | 0o200)

def _unique_runtime_macho_files(runtime: Path) -> list[Path]:
    seen: set[Path] = set()
    files: list[Path] = []
    runtime_resolved = runtime.resolve()
    for path in runtime.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(runtime_resolved):
            raise RuntimePrepError(f"Mach-O candidate escapes staged runtime: {path}")
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_macho_file(resolved):
            files.append(resolved)
    return files

def _add_rpath_if_missing(path: Path, rpath: str) -> None:
    existing = _parse_otool_rpaths(_otool(["otool", "-l", str(path)]))
    if rpath not in existing:
        _ensure_owner_write(path)
        _install_name_tool(["-add_rpath", rpath, str(path)])

def _normalize_stale_libpython_loads(runtime: Path, old_id: str, normalized_id: str, major: int, minor: int) -> None:
    bin_dir = (runtime / "bin").resolve()
    dynload_dir = (runtime / "lib" / f"python{major}.{minor}" / "lib-dynload").resolve()
    for path in _unique_runtime_macho_files(runtime):
        deps = _otool_load_deps(path)
        if old_id not in deps:
            continue
        if deps.count(old_id) > 1:
            raise RuntimePrepError(f"duplicate stale libpython load command in {path}")
        if path.parent.resolve() == bin_dir:
            required_rpath = "@executable_path/../lib"
        elif path.parent.resolve() == dynload_dir:
            required_rpath = "@loader_path/../.."
        else:
            raise RuntimePrepError(f"stale libpython dependency has ambiguous runtime-relative layout: {path}")
        _ensure_owner_write(path)
        _install_name_tool(["-change", old_id, normalized_id, str(path)])
        _add_rpath_if_missing(path, required_rpath)
        if old_id in _otool_load_deps(path):
            raise RuntimePrepError(f"failed to normalize stale libpython dependency in {path}")

def normalize_python_build_standalone_macos_runtime(runtime: Path, manifest: dict) -> None:
    if platform.system() != "Darwin":
        return
    major, minor = _python_major_minor(manifest)
    libpython = runtime / "lib" / f"libpython{major}.{minor}.dylib"
    if not libpython.is_file():
        raise RuntimePrepError(f"missing bundled libpython dylib: {libpython}")
    old_id = f"/install/lib/libpython{major}.{minor}.dylib"
    normalized_id = f"@rpath/libpython{major}.{minor}.dylib"
    current = _otool_install_id(libpython)
    if current == old_id:
        _ensure_owner_write(libpython)
        _install_name_tool(["-id", normalized_id, str(libpython)])
        reread = _otool_install_id(libpython)
        if reread != normalized_id:
            raise RuntimePrepError(f"failed to normalize libpython install ID: {reread}")
    elif current != normalized_id:
        raise RuntimePrepError(f"unexpected libpython install ID: {current}")
    _normalize_stale_libpython_loads(runtime, old_id, normalized_id, major, minor)

def _is_macho_file(path: Path) -> bool:
    if platform.system() != "Darwin":
        return False
    result = subprocess.run(["file", str(path)], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimePrepError(f"file inspection failed for {path}: {result.stderr.strip()}")
    return "Mach-O" in result.stdout

def _otool(cmd: list[str]) -> str:
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimePrepError(f"native linkage inspection failed ({' '.join(cmd)}): {result.stderr.strip()}")
    return result.stdout

def _allowed_runtime_ref(value: str) -> bool:
    return value.startswith(("@loader_path", "@rpath", "@executable_path", "/usr/lib/", "/System/Library/"))

def _forbidden_native_ref(value: str) -> bool:
    lower = value.lower()
    markers = (
        "/opt/homebrew", "/usr/local/cellar", "pyenv", "commandlinetools",
        "/applications/xcode.app", "python.framework", "/users/runner",
        "/private/var/folders", "/" + "tmp/", "/var/" + "tmp/", "token-place-python-runtime-",
        "libssl.3.dylib", "libcrypto.3.dylib",
    )
    return any(marker in lower for marker in markers)

def _validate_native_ref(value: str, owner: Path, *, install_id: bool = False, rpath: bool = False) -> None:
    if not value:
        return
    if _forbidden_native_ref(value):
        raise RuntimePrepError(f"forbidden Mach-O reference in {owner}: {value}")
    if value.startswith("/") and not value.startswith(("/usr/lib/", "/System/Library/")):
        raise RuntimePrepError(f"absolute non-system Mach-O reference in {owner}: {value}")
    if install_id and value.startswith("/"):
        raise RuntimePrepError(f"absolute Mach-O install ID in {owner}: {value}")
    if rpath and not value.startswith(("@loader_path", "@executable_path")):
        raise RuntimePrepError(f"non-relocatable Mach-O LC_RPATH in {owner}: {value}")

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

def _macho_file_kind(file_description: str) -> str:
    lower = file_description.lower()
    if "dynamically linked shared library" in lower or "dylib" in lower:
        return "dylib"
    if "bundle" in lower:
        return "bundle"
    if "executable" in lower:
        return "executable"
    return "other"

def audit_macho_runtime(runtime: Path) -> None:
    if platform.system() != "Darwin":
        return
    for path in runtime.rglob("*"):
        if not path.is_file():
            continue
        result = subprocess.run(["file", str(path)], text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise RuntimePrepError(f"file inspection failed for {path}: {result.stderr.strip()}")
        if "Mach-O" not in result.stdout:
            continue
        file_description = result.stdout
        archs = _otool(["lipo", "-archs", str(path)])
        if "arm64" not in archs:
            raise RuntimePrepError(f"Mach-O file is not arm64: {path}")
        deps = _otool(["otool", "-L", str(path)]).splitlines()[1:]
        for dep_line in deps:
            dep = dep_line.strip().split(" ", 1)[0]
            _validate_native_ref(dep, path)
        load_commands = _otool(["otool", "-l", str(path)])
        install_ids = _parse_otool_install_ids(load_commands)
        kind = _macho_file_kind(file_description)
        if kind == "dylib" and len(install_ids) != 1:
            raise RuntimePrepError(f"Mach-O dylib is missing LC_ID_DYLIB: {path}")
        for install_id in install_ids:
            _validate_native_ref(install_id, path, install_id=True)
        for rpath in _parse_otool_rpaths(load_commands):
            _validate_native_ref(rpath, path, rpath=True)

def _uninstall_llama_cpp(py: Path) -> None:
    run([str(py), "-m", "pip", "uninstall", "-y", "llama-cpp-python"])
    code = """
import importlib.util, pathlib, sysconfig
site = pathlib.Path(sysconfig.get_paths()['purelib'])
stale=[]
for pattern in ('llama_cpp*','libllama*','libggml*','libmtmd*'):
    stale.extend(str(p) for p in site.rglob(pattern))
if importlib.util.find_spec('llama_cpp') is not None or stale:
    raise SystemExit('stale llama-cpp-python files remain: ' + ','.join(stale[:20]))
"""
    run([str(py), "-c", code])

def _validate_candidate_install(py: Path, m: dict, runtime: Path) -> None:
    run([str(py), "-m", "pip", "check"])
    run([str(py), "-c", "import " + ",".join(IMPORTS)])
    probe_runtime(py, m)
    audit_macho_runtime(runtime)

def install_packages(py: Path, m: dict, pip_cache: Path) -> None:
    run([str(py), "-m", "ensurepip", "--upgrade"])
    run([str(py), "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"], env={"PIP_CACHE_DIR": str(pip_cache)})
    req = SRC_TAURI / "python" / "requirements_desktop_runtime.txt"
    pinned_packages = [f"{name}=={version}" for name, version in sorted(m["required_packages"].items()) if name != "llama-cpp-python"]
    # Install all non-llama packages first, independently of the llama wheel index.
    run([str(py), "-m", "pip", "install", "-r", str(req), *pinned_packages, "--upgrade", "--no-cache-dir"], env={"PIP_CACHE_DIR": str(pip_cache)})
    # Try each Metal-capable plan in order: prebuilt wheel first, then Metal source build.
    plans = llama_cpp_install_plan_fallbacks(platform="darwin", requirements_path=ROOT.parent / "requirements.txt")
    metal_plans = [p for p in plans if p.backend == "metal"]
    if not metal_plans:
        raise RuntimePrepError("no Metal install plan available for darwin")
    expected_spec = f"llama-cpp-python=={m['required_packages']['llama-cpp-python']}"
    last_err: Exception | None = None
    for plan in metal_plans:
        if plan.package_spec != expected_spec:
            raise RuntimePrepError(f"install plan package spec mismatch: expected {expected_spec}, got {plan.package_spec}")
        env = {"PIP_CACHE_DIR": str(pip_cache), **plan.pip_env()}
        if getattr(plan, "force_cmake", False):
            env.update({
                "CMAKE_OSX_ARCHITECTURES": m["expected_architecture"],
                "CMAKE_OSX_DEPLOYMENT_TARGET": m["minimum_macos_version"],
                "MACOSX_DEPLOYMENT_TARGET": m["minimum_macos_version"],
            })
        try:
            run([str(py), "-m", "pip", "install", *plan.pip_install_args(), plan.package_spec], env=env)
            _validate_candidate_install(py, m, py.parents[1])
            last_err = None
            break
        except (subprocess.CalledProcessError, RuntimePrepError) as e:
            last_err = e
            try:
                _uninstall_llama_cpp(py)
            except Exception as uninstall_error:
                last_err = RuntimePrepError(f"{e}; additionally failed to remove rejected llama-cpp-python: {uninstall_error}")
    if last_err is not None:
        raise RuntimePrepError(f"failed to install {expected_spec} with any relocatable Metal plan: {last_err}")

def _missing_runtime_capabilities(payload: dict) -> list[str]:
    top_level = {
        "rope_scaling_type": "rope_scaling_type_supported",
        "rope_freq_scale": "rope_freq_scale_supported",
        "yarn_orig_ctx": "yarn_orig_ctx_supported",
    }
    constructor = (payload.get("constructor_kwarg_support") or {})
    missing = []
    if payload.get("qwen_64k_yarn_support") != "supported":
        missing.append("qwen_64k_yarn_support")
    missing.extend(name for name, field in top_level.items() if not payload.get(field))
    missing.extend(
        name
        for name in ("flash_attn", "offload_kqv", "n_batch", "n_ubatch")
        if not constructor.get(name)
    )
    return missing

def probe_runtime(py: Path, m: dict) -> dict:
    code = "import json,importlib.metadata as im; from desktop_runtime_setup import _probe_llama_runtime; p=_probe_llama_runtime(); print(json.dumps(p.__dict__))"
    env = {"PYTHONPATH": str(SRC_TAURI / "python") + os.pathsep + str(SRC_TAURI.parent.parent)}
    payload = json.loads(run([str(py), "-c", code], env=env).stdout)
    if payload.get("backend") != "metal" or not payload.get("gpu_offload_supported"): raise RuntimePrepError("embedded llama_cpp runtime is not Metal-capable")
    if payload.get("llama_cpp_python_version") != m["required_packages"]["llama-cpp-python"]: raise RuntimePrepError("wrong llama-cpp-python version")
    missing = _missing_runtime_capabilities(payload)
    if missing: raise RuntimePrepError("missing Qwen 64K runtime capabilities: " + ", ".join(missing))
    return payload

def clean(runtime: Path) -> None:
    for p in runtime.rglob("*"):
        if p.is_dir() and p.name in {"__pycache__", "tests", "test"}: shutil.rmtree(p, ignore_errors=True)
        elif p.is_file() and (p.suffix == ".pyc" or p.name.endswith(".pyo")): p.unlink(missing_ok=True)

def provenance(m: dict, packages: dict) -> dict:
    try: commit = subprocess.check_output(["git","rev-parse","HEAD"], cwd=ROOT.parent, text=True).strip()
    except Exception: commit = "unknown"
    return {"cpython_version":m["cpython_version"],"target_triple":m["target_triple"],"source_archive_sha256":m["sha256"],"installed_packages":packages,"expected_backend":"metal","build_profile":BUILD_PROFILE,"build_timestamp":datetime.now(timezone.utc).isoformat(),"repository_commit":commit}

def existing_valid(m: dict) -> bool:
    prov = OUTPUT / PROVENANCE; py = OUTPUT / "bin" / "python3"
    if not prov.is_file() or not py.is_file(): return False
    try:
        data=json.loads(prov.read_text());
        if data.get("source_archive_sha256") != m["sha256"] or data.get("expected_backend") != "metal" or data.get("build_profile") != BUILD_PROFILE: return False
        installed = data.get("installed_packages") or {}
        for name, version in m["required_packages"].items():
            if installed.get(name) != version:
                return False
        prove_interpreter(py, OUTPUT, m); probe_runtime(py, m); audit_macho_runtime(OUTPUT); return True
    except Exception: return False

def prepare(cache_dir: Path) -> None:
    m=load_manifest()
    if existing_valid(m): print("embedded runtime already valid"); return
    archive=download_verified(m, cache_dir)
    with tempfile.TemporaryDirectory(prefix="token-place-python-runtime-", dir=str(OUTPUT.parent)) as td:
        tmp=Path(td); extracted=extract_archive(archive,m,tmp); staging=tmp/"python-runtime"; shutil.move(str(extracted), staging)
        py=staging/"bin"/"python3"; py.chmod(py.stat().st_mode | 0o755)
        normalize_python_build_standalone_macos_runtime(staging, m); audit_macho_runtime(staging); prove_interpreter(py, staging, m); install_packages(py, m, cache_dir/"pip"); probe_runtime(py, m); clean(staging); audit_macho_runtime(staging)
        packages=json.loads(run([str(py),"-c","import json,importlib.metadata as im; print(json.dumps({d.metadata['Name']: d.version for d in im.distributions()}))"]).stdout)
        (staging/PROVENANCE).write_text(json.dumps(provenance(m, packages), indent=2, sort_keys=True)+"\n")
        for notice in m["runtime_notices"]: (staging/notice["path"]).write_text(f"{notice['name']} redistribution notice: {notice['license']}\nSee upstream distribution for complete license text.\n")
        backup=tmp/"old-runtime"
        if OUTPUT.exists(): OUTPUT.rename(backup)
        staging.rename(OUTPUT); shutil.rmtree(backup, ignore_errors=True)

def main() -> int:
    ap=argparse.ArgumentParser(); ap.add_argument("--cache-dir", type=Path, default=Path(os.environ.get("TOKEN_PLACE_EMBEDDED_PYTHON_CACHE", Path.home()/".cache/token-place/embedded-python")))
    args=ap.parse_args()
    try: prepare(args.cache_dir); return 0
    except subprocess.CalledProcessError as e:
        print(f"embedded runtime preparation failed: {e}", file=sys.stderr)
        if e.stdout: print(e.stdout, file=sys.stderr)
        if e.stderr: print(e.stderr, file=sys.stderr)
        return 1
    except Exception as e: print(f"embedded runtime preparation failed: {e}", file=sys.stderr); return 1
if __name__ == "__main__": raise SystemExit(main())
