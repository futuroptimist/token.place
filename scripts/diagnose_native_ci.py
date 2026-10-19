#!/usr/bin/env python3
"""Linux CI-only native fingerprints and a bounded, prompt-free model-load probe.

No installs, downloads, inference, environment dumps, raw native logs, or cores.
Probe failure is evidence, not a replacement for the existing E2E guardrail.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zipfile


MODEL_SHA256 = "6151b1929d7f5aa3385d9ddef3393e55587c0a55de661562322bc51dfda93a04"
STAGES = {"import_started", "import_ok", "features_started", "features_ok",
          "load_started", "load_ok", "close_ok"}
PROBE_CODE = r'''
import json, os, re, sys
fd = int(sys.argv[2])
def emit(stage, features=None):
    data = {"stage": stage}
    if features is not None:
        data["features"] = features
    os.write(fd, (json.dumps(data) + "\n").encode())
emit("import_started")
import llama_cpp
emit("import_ok")
emit("features_started")
info = llama_cpp.llama_print_system_info().decode("ascii", errors="replace")
features = dict(re.findall(r"\b([A-Z][A-Z0-9_]{0,40})\s*=\s*([01])\b", info))
emit("features_ok", features)
emit("load_started")
model = llama_cpp.Llama(model_path=sys.argv[1], n_ctx=512,
                       n_gpu_layers=0, n_threads=1, verbose=False)
emit("load_ok")
model.close()
emit("close_ok")
'''


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cpu_details(text):
    """Keep CPU identity/ISA only, including differences across logical CPUs."""
    allowed = {"vendor_id", "cpu family", "model", "model name", "stepping", "flags"}
    records = set()
    for block in text.split("\n\n"):
        values = {}
        for line in block.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() in allowed:
                values[key.strip()] = value.strip()
        if values:
            records.add(json.dumps(values, sort_keys=True))
    return [json.loads(record) for record in sorted(records)]


def native_fingerprint(path):
    result = {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
    readelf = shutil.which("readelf")
    if readelf:
        notes = subprocess.run(
            [readelf, "--notes", str(path)], capture_output=True,
            text=True, check=False, timeout=10,
        )
        result["elf_build_ids"] = re.findall(r"Build ID: ([0-9a-f]+)", notes.stdout)
    return result


def installed_fingerprints():
    dist = importlib.metadata.distribution("llama-cpp-python")
    native = []
    metadata = {}
    for entry in dist.files or []:
        path = Path(dist.locate_file(entry))
        if path.is_file() and ".so" in path.name:
            fingerprint = native_fingerprint(path)
            fingerprint["name"] = str(entry)
            native.append(fingerprint)
        if path.is_file() and path.name in {"WHEEL", "METADATA", "RECORD"}:
            metadata[path.name] = sha256(path)
    return {"version": dist.version, "metadata_sha256": metadata, "native": native,
            "compiler_flags": "Not recorded in installed wheel metadata; do not infer from host CPU."}


def cached_fingerprints(cache):
    """Hash only llama wheels; never emit cache paths or origin URLs."""
    result = []
    for path in sorted(cache.glob("wheels/**/llama_cpp_python-*.whl")):
        native = {}
        with zipfile.ZipFile(path) as wheel:
            for name in wheel.namelist():
                if ".so" in Path(name).name:
                    with wheel.open(name) as stream:
                        native[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        result.append({"name": path.name, "sha256": sha256(path), "native_sha256": native})
    return result


def run_probe(model, timeout):
    # -I avoids the repository's llama_cpp.py shim and PYTHONPATH/user packages.
    # The child inherits no credentials/configuration, has no stdin, and cannot
    # persist a core. A private descriptor carries only allowlisted status data.
    with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as events:
        code = "import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n" + PROBE_CODE
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", code, str(model), str(events.fileno())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"}, cwd=directory,
            pass_fds=(events.fileno(),), start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        events.seek(0)
        stages = []
        features = {}
        for line in events.read(16384).splitlines():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or not isinstance(event.get("stage"), str):
                continue
            if event["stage"] not in STAGES:
                continue
            stages.append(event["stage"])
            raw_features = event.get("features", {})
            if not isinstance(raw_features, dict):
                continue
            for key, value in raw_features.items():
                if (re.fullmatch(r"[A-Z][A-Z0-9_]{0,40}", key)
                        and isinstance(value, str) and value in {"0", "1"}):
                    features[key] = value
    return {"returncode": process.returncode, "timed_out": timed_out,
            "signal": signal.Signals(-process.returncode).name if process.returncode < 0 else None,
            "stages": stages, "compiled_features": features,
            "loaded": "load_ok" in stages, "closed": "close_ok" in stages,
            "parameters": {"n_ctx": 512, "n_gpu_layers": 0, "n_threads": 1},
            "scope": "Isolated CPU constructor only; no generation or application routing."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--pip-cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if not 0 < args.timeout <= 120:
        parser.error("timeout must be greater than zero and at most 120 seconds")
    report = {"schema": 1, "python": platform.python_version(),
              "machine": platform.machine(), "errors": {}}

    def collect(name, callback):
        try:
            report[name] = callback()
        except Exception:
            # Exception messages can contain private paths or configuration.
            # Keep other evidence and use only a fixed, non-secret status.
            report[name] = None
            report["errors"][name] = "collection_failed"

    collect("libc", platform.libc_ver)
    collect("cpu", lambda: cpu_details(Path("/proc/cpuinfo").read_text()))
    collect("installed", installed_fingerprints)
    collect("cached_wheels", lambda: cached_fingerprints(args.pip_cache))
    collect("model_sha256", lambda: sha256(args.model))
    if report["model_sha256"] is None:
        report["probe"] = {"status": "not_run_model_checksum_unavailable"}
    elif report["model_sha256"] != MODEL_SHA256:
        report["probe"] = {"status": "not_run_model_checksum_mismatch"}
    else:
        collect("probe", lambda: run_probe(args.model.resolve(), args.timeout))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    # Observational only. The unchanged real-model guardrail remains mandatory.


if __name__ == "__main__":
    main()
