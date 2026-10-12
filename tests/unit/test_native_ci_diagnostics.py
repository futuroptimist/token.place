"""Exercise diagnostic isolation and failure reporting without a native model."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
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
    pidfile = tmp_path / "pid"
    monkeypatch.setattr(diagnostics, "PROBE_CODE", f'''
import os, pathlib, time
pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))
time.sleep(30)
''')
    result = diagnostics.run_probe(tmp_path / "unused.gguf", 0.5)
    assert result["timed_out"] and result["signal"] == "SIGKILL"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


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
