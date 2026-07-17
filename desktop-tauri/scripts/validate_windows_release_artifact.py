#!/usr/bin/env python3
"""Validate Windows desktop release artifact version and bundled CUDA runtime inventory."""
from __future__ import annotations
import argparse, json, re, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "src-tauri" / "python" / "embedded_python_runtime_manifest_windows_x86_64.json"
EXPECTED_VERSION = "0.1.2"
REQUIRED_ENTRIES = [
    "python-runtime/python.exe",
    "python-runtime/python311.dll",
    "python-runtime/embedded_python_runtime_provenance.json",
    "python-runtime/llama_cpp",
    "python-runtime/llama_cpp_python-0.3.32.dist-info",
]

def _names(path: Path) -> list[str]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    return [str(p.relative_to(path)).replace('\\','/') for p in path.rglob('*')] if path.is_dir() else []

def validate(path: Path) -> None:
    if EXPECTED_VERSION not in path.name:
        raise SystemExit(f"artifact filename must contain {EXPECTED_VERSION}: {path.name}")
    manifest = json.loads(MANIFEST.read_text())
    wheel = manifest["llama_cpp_python_wheel"]
    if wheel["version"] != "0.3.32" or wheel["cuda"] != "cu124" or wheel["platform_tag"] != "win_amd64":
        raise SystemExit("manifest wheel flavor is not pinned 0.3.32 cu124 win_amd64")
    if re.fullmatch(r"0{64}|1{64}", wheel["sha256"]):
        raise SystemExit("manifest contains placeholder wheel sha256")
    names = _names(path)
    if not names:
        raise SystemExit("artifact must be a directory or zip-compatible extracted installer payload")
    missing = [entry for entry in REQUIRED_ENTRIES if not any(entry in name for name in names)]
    dll_missing = [dll for dll in manifest["required_native_dlls"] if not any(name.endswith(dll) for name in names)]
    if missing or dll_missing:
        raise SystemExit(f"missing runtime entries={missing} dlls={dll_missing}")

if __name__ == "__main__":
    ap=argparse.ArgumentParser(); ap.add_argument("artifact", type=Path)
    validate(ap.parse_args().artifact)
