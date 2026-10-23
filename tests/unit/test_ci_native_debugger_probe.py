"""Synthetic probe tests: never launch a debugger or native backend locally."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "probe", ROOT / "scripts/ci_native_debugger_probe.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def enable(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "linux")
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_RUN_ATTEMPT": "1",
        "TOKEN_PLACE_PROBE_SUBJECT": probe.PROBE_SUBJECT,
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    "gate",
    [
        "GITHUB_ACTIONS",
        "RUNNER_ENVIRONMENT",
        "GITHUB_RUN_ATTEMPT",
        "TOKEN_PLACE_PROBE_SUBJECT",
    ],
)
def test_each_gate_refuses_before_debugger_discovery(
    monkeypatch, tmp_path, gate
):
    enable(monkeypatch)
    monkeypatch.setenv(gate, "incorrect")
    monkeypatch.setattr(
        probe.shutil, "which", lambda _: pytest.fail("discovery")
    )
    with pytest.raises(SystemExit):
        probe.run_probe(tmp_path / "report.json")


def test_missing_debugger_stops_without_acquisition(monkeypatch, tmp_path):
    enable(monkeypatch)
    monkeypatch.setattr(probe.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        probe.subprocess, "Popen", lambda *a, **k: pytest.fail("launch")
    )
    output = tmp_path / "report.json"
    probe.run_probe(output)
    assert json.loads(output.read_text()) == {"status": "debugger_unavailable"}


@pytest.mark.parametrize("timeout", [False, True])
def test_probe_isolated_and_group_cleaned(monkeypatch, tmp_path, timeout):
    enable(monkeypatch)
    monkeypatch.setenv("SECRET_FIXTURE", "must-not-inherit")
    monkeypatch.setattr(probe.shutil, "which", lambda _: "/usr/bin/gdb")
    killed = []
    monkeypatch.setattr(
        probe.os, "killpg", lambda *args: killed.append(args), raising=False
    )
    monkeypatch.setattr(probe.signal, "SIGKILL", 9, raising=False)

    class Process:
        pid = 54321
        returncode = 0

        def __init__(self, command, **kwargs):
            assert "--nx" in command
            assert "set auto-load off" in command
            assert "set disable-randomization off" in command
            assert "set debuginfod enabled off" in command
            assert kwargs["start_new_session"] is True
            assert set(kwargs["env"]) == {
                "PATH",
                "HOME",
                "LANG",
                "DEBUGINFOD_URLS",
            }
            assert (
                kwargs["stdout"]
                == kwargs["stderr"]
                == probe.subprocess.DEVNULL
            )
            (kwargs["cwd"] / "probe-result.json").write_text(
                '{"status":"SIGILL"}'
            )

        def wait(self, timeout):
            if timeout == 90 and globals_timeout:
                self.returncode = None
                raise probe.subprocess.TimeoutExpired("gdb", timeout)

    globals_timeout = timeout
    monkeypatch.setattr(probe.subprocess, "Popen", Process)
    output = tmp_path / "report.json"
    probe.run_probe(output)
    assert killed == ([(54321, 9)] if timeout else [])
    assert json.loads(output.read_text())["status"] == (
        "debugger_timeout" if timeout else "SIGILL"
    )


def test_capture_uses_bounded_structured_fields_not_arguments():
    compile(probe.GDB_SCRIPT, "capture.py", "exec")
    assert "range(16)" in probe.GDB_SCRIPT
    assert "count=8" in probe.GDB_SCRIPT
    assert "read_var" not in probe.GDB_SCRIPT
    assert "backtrace" not in probe.GDB_SCRIPT
    assert "RLIMIT_CORE, (0, 0)" in probe.INFERIOR


@pytest.mark.parametrize(
    "session_matches,start_matches",
    [(True, True), (False, True), (True, False)],
)
def test_separate_group_inferior_requires_session_and_start_identity(
    monkeypatch, tmp_path, session_matches, start_matches
):
    # Synthetic recorded inferior with its own process group.
    record = {"pid": 222, "session": 111, "start": "777"}
    (tmp_path / "owned-inferior.json").write_text(json.dumps(record))
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).replace("\\", "/") == "/proc/222/stat":
            fields = ["0"] * 20
            fields[2] = "222"  # pgrp differs from debugger group111
            fields[19] = "777" if start_matches else "888"
            return "222 (fixture) " + " ".join(fields)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(
        probe.os,
        "getsid",
        lambda pid: 111 if session_matches else 999,
        raising=False,
    )
    monkeypatch.setattr(probe.signal, "SIGKILL", 9, raising=False)
    killed = []
    monkeypatch.setattr(probe.os, "pidfd_open", lambda pid: 333, raising=False)
    monkeypatch.setattr(probe.os, "close", lambda fd: None)
    monkeypatch.setattr(
        probe.signal,
        "pidfd_send_signal",
        lambda *args: killed.append(args),
        raising=False,
    )
    result = probe.cleanup_inferior(tmp_path, 111)
    if session_matches and start_matches:
        assert result == "owned_inferior_signal_sent" and killed == [(333, 9)]
    else:
        assert result == "identity_unverified" and killed == []


def test_workflow_one_shot_gate_and_inference_remain_intact():
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 2
    step = next(
        s
        for s in steps
        if s.get("name") == "One approved disposable native debugger probe"
    )
    assert "steps.native_inference.outcome == 'failure'" in step["if"]
    assert "github.run_attempt == 1" in step["if"]
    assert probe.PROBE_SUBJECT in step["run"]
    assert 'git show -s --format=%s "$PROBE_HEAD"' in step["run"]
    assert "continue-on-error" not in next(
        s for s in steps if s.get("id") == "native_inference"
    )
