"""Exercise diagnostic isolation and failure reporting without a native model."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import sys
from types import SimpleNamespace
import zipfile

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "diagnose_native_ci.py"
SPEC = importlib.util.spec_from_file_location("native_ci_diagnostics", SCRIPT)
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux CI diagnostics")


def test_cpu_details_excludes_unrelated_fields():
    text = "vendor_id : GenuineIntel\nflags : sse avx2\nserial : private\n"
    assert diagnostics.cpu_details(text + "\n" + text) == [
        {"vendor_id": "GenuineIntel", "flags": "sse avx2"}
    ]


def test_probe_isolated_and_discards_raw_output(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    (tmp_path / "llama_cpp.py").write_text("raise RuntimeError('shim')")
    # Use JSON encoding to keep the child descriptor's newline unambiguous.
    monkeypatch.setattr(diagnostics, "PROBE_CODE", '''
import json, os, sys
assert 'GH_TOKEN' not in os.environ and 'PYTHONPATH' not in os.environ
assert sys.flags.isolated and '' not in sys.path
print('must-not-leak')
print('must-not-leak', file=sys.stderr)
data = {"stage": "load_ok", "features": {"AVX2": "1", "SECRET": "must-not-leak"},
        "ignored": "must-not-leak"}
os.write(int(sys.argv[2]), (json.dumps(data) + chr(10)).encode())
''')
    result = diagnostics.run_probe(tmp_path / "unused.gguf", 5)
    assert result["returncode"] == 0
    assert result["loaded"]
    assert result["compiled_features"] == {"AVX2": "1"}
    assert "must-not-leak" not in json.dumps(result)


def test_probe_reports_sigill_without_core(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "PROBE_CODE", '''
import os, resource, signal, sys
assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)
os.write(int(sys.argv[2]), b'{"stage":"load_started"}' + bytes([10]))
os.kill(os.getpid(), signal.SIGILL)
''')
    result = diagnostics.run_probe(tmp_path / "unused.gguf", 5)
    assert result["returncode"] == -signal.SIGILL
    assert result["signal"] == "SIGILL"
    assert result["stages"] == ["load_started"]
    assert not result["loaded"] and not result["timed_out"]


def test_probe_timeout_reaps_process(tmp_path, monkeypatch):
    children = []
    original_popen = diagnostics.subprocess.Popen

    def capture_child(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(diagnostics.subprocess, "Popen", capture_child)
    monkeypatch.setattr(diagnostics, "PROBE_CODE", "import time; time.sleep(30)")
    result = diagnostics.run_probe(tmp_path / "unused.gguf", 0.5)
    assert result["timed_out"] and result["signal"] == "SIGKILL"
    assert len(children) == 1 and children[0].returncode == -signal.SIGKILL
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)


def test_cached_fingerprints_hash_bytes_without_origin_urls(tmp_path):
    cache = tmp_path / "wheels" / "ab"
    cache.mkdir(parents=True)
    wheel = cache / "llama_cpp_python-0.3.32-py3-none-linux_x86_64.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("llama_cpp/lib/libllama.so", b"native-bytes")
    (cache / "origin.json").write_text('{"url":"https://secret@example.invalid"}')
    result = diagnostics.cached_fingerprints(tmp_path)
    assert result[0]["sha256"] == diagnostics.sha256(wheel)
    assert result[0]["native_sha256"]["llama_cpp/lib/libllama.so"] == diagnostics.hashlib.sha256(b"native-bytes").hexdigest()
    assert "secret" not in json.dumps(result)


def test_model_checksum_mismatch_prevents_probe(tmp_path, monkeypatch):
    model = tmp_path / "wrong.gguf"
    model.write_bytes(b"wrong")
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--model", str(model),
                                      "--pip-cache", str(tmp_path), "--output", str(output)])
    monkeypatch.setattr(diagnostics, "installed_fingerprints", lambda: {})
    def unexpected_probe(*args):
        pytest.fail("unverified model must not be loaded")
    monkeypatch.setattr(diagnostics, "run_probe", unexpected_probe)
    diagnostics.main()
    assert json.loads(output.read_text())["probe"]["status"] == "not_run_model_checksum_mismatch"


def test_installed_fingerprints_preserve_identity_without_metadata_contents(tmp_path, monkeypatch):
    entries = [Path(name) for name in (
        "llama_cpp/lib/libllama.so", "lib/libllama.so", "package.dist-info/WHEEL",
        "package.dist-info/METADATA", "package.dist-info/RECORD",
        "package.dist-info/direct_url.json", "missing.so",
    )]
    for entry in entries[:-1]:
        path = tmp_path / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private-metadata-content")
    # An existing ELF is enough to exercise real readelf without building or
    # importing a native library. The second same-named file has different bytes.
    shutil.copyfile(sys.executable, tmp_path / entries[0])
    distribution = SimpleNamespace(
        version="fixture", files=entries, locate_file=lambda entry: tmp_path / entry,
    )
    monkeypatch.setattr(diagnostics.importlib.metadata, "distribution", lambda _: distribution)
    report = diagnostics.installed_fingerprints()
    libraries = {entry["name"]: entry for entry in report["native"]}
    assert set(libraries) == {str(entries[0]), str(entries[1])}
    assert libraries[str(entries[0])]["sha256"] == diagnostics.sha256(Path(sys.executable))
    assert libraries[str(entries[0])]["sha256"] != libraries[str(entries[1])]["sha256"]
    assert set(report["metadata_sha256"]) == {"WHEEL", "METADATA", "RECORD"}
    assert report["metadata_sha256"]["WHEEL"] == diagnostics.sha256(tmp_path / entries[2])
    assert "private-metadata-content" not in json.dumps(report)
    assert str(tmp_path) not in json.dumps(report)
    if diagnostics.shutil.which("readelf"):
        assert isinstance(libraries[str(entries[0])]["elf_build_ids"], list)


def test_verified_model_dispatches_one_bounded_probe(tmp_path, monkeypatch):
    model = tmp_path / "fixture.gguf"
    model.write_bytes(b"fixture-model")
    output = tmp_path / "report.json"
    monkeypatch.setattr(diagnostics, "MODEL_SHA256", diagnostics.sha256(model))
    monkeypatch.setattr(diagnostics, "installed_fingerprints", lambda: {})
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--model", str(model),
                                      "--pip-cache", str(tmp_path), "--output", str(output)])
    calls = []

    def probe(path, timeout):
        calls.append((path, timeout))
        return {"loaded": True, "returncode": 0}

    monkeypatch.setattr(diagnostics, "run_probe", probe)
    diagnostics.main()
    assert calls == [(model.resolve(), 90)]
    assert json.loads(output.read_text())["probe"] == {"loaded": True, "returncode": 0}


def test_probe_filters_malformed_and_unrecognized_status_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "PROBE_CODE", '''
import json, os, sys
fd = int(sys.argv[2])
os.write(fd, b'not-json' + bytes([10]))
for frame in [None, {"stage": ["private"]}, {"stage": "private"},
              {"stage": "import_ok", "features": ["private"]},
              {"stage": "load_ok", "features": {"AVX2": ["private"]}}]:
    os.write(fd, (json.dumps(frame) + chr(10)).encode())
''')
    result = diagnostics.run_probe(tmp_path / "unused.gguf", 5)
    assert result["returncode"] == 0
    assert result["stages"] == ["import_ok", "load_ok"]
    assert result["compiled_features"] == {}
    assert "private" not in json.dumps(result)


def test_corrupt_cache_keeps_other_evidence_and_runs_verified_probe(tmp_path, monkeypatch):
    model = tmp_path / "fixture.gguf"
    model.write_bytes(b"fixture-model")
    cache = tmp_path / "wheels" / "ab"
    cache.mkdir(parents=True)
    (cache / "llama_cpp_python-unused.whl").write_bytes(b"corrupt-private-bytes")
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--model", str(model),
                                      "--pip-cache", str(tmp_path), "--output", str(output)])
    monkeypatch.setattr(diagnostics, "MODEL_SHA256", diagnostics.sha256(model))
    monkeypatch.setattr(diagnostics, "installed_fingerprints", lambda: {"version": "fixture"})
    calls = []

    def probe(path, timeout):
        calls.append((path, timeout))
        return {"loaded": True, "returncode": 0}

    monkeypatch.setattr(diagnostics, "run_probe", probe)
    diagnostics.main()
    report = json.loads(output.read_text())
    assert report["errors"] == {"cached_wheels": "collection_failed"}
    assert report["cached_wheels"] is None
    assert report["installed"] == {"version": "fixture"}
    assert report["cpu"] and report["model_sha256"] == diagnostics.sha256(model)
    assert report["probe"]["loaded"] and calls == [(model.resolve(), 90)]
    assert "corrupt-private-bytes" not in output.read_text()


def test_collection_errors_are_redacted_and_missing_checksum_blocks_probe(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--model", str(tmp_path / "missing.gguf"),
                                      "--pip-cache", str(tmp_path), "--output", str(output)])

    def broken_fingerprint():
        raise RuntimeError("private-credential-and-path")

    def unexpected_probe(*args):
        pytest.fail("a missing model checksum must prevent loading")

    monkeypatch.setattr(diagnostics, "installed_fingerprints", broken_fingerprint)
    monkeypatch.setattr(diagnostics, "run_probe", unexpected_probe)
    diagnostics.main()
    report = json.loads(output.read_text())
    assert report["errors"] == {"installed": "collection_failed", "model_sha256": "collection_failed"}
    assert report["installed"] is None and report["cpu"] and report["cached_wheels"] == []
    assert report["probe"] == {"status": "not_run_model_checksum_unavailable"}
    assert "private-credential-and-path" not in output.read_text()
