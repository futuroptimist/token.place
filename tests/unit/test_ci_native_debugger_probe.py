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
    gate = next(s for s in steps if s.get("id") == "probe_gate")
    step = next(
        s
        for s in steps
        if s.get("name") == "One approved disposable native debugger probe"
    )
    assert "steps.native_inference.outcome == 'failure'" in gate["if"]
    assert "github.run_attempt == 1" in gate["if"]
    assert "github.event.pull_request.number == 1927" in gate["if"]
    assert "github.event.action == 'synchronize'" in gate["if"]
    assert probe.PROBE_SUBJECT in gate["run"]
    assert 'git show -s --format=%s "$PROBE_HEAD"' in gate["run"]
    assert "steps.gdb_setup.outcome == 'success'" in step["if"]
    assert step["timeout-minutes"] == 10
    setup = next(s for s in steps if s.get("id") == "gdb_setup")
    assert setup["timeout-minutes"] == 10
    assert "steps.probe_gate.outputs.enabled == 'true'" in setup["if"]
    assert "Acquire::Retries=0" in setup["run"]
    assert "--no-upgrade --no-remove" in setup["run"]
    assert "Dir::Etc::sourceparts=-" in setup["run"]
    assert "--kill-after=5s 580s" in setup["run"]
    assert workflow["jobs"]["test"]["timeout-minutes"] == 30
    upload = next(
        s
        for s in steps
        if s.get("name") == "Retain bounded native inference diagnostics"
    )
    assert "steps.diagnostic_budget.outcome == 'success'" in upload["if"]
    assert "continue-on-error" not in next(
        s for s in steps if s.get("id") == "native_inference"
    )


SOURCE = """Types: deb
URIs: http://azure.archive.ubuntu.com/ubuntu/
Suites: noble noble-updates
Components: main universe
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""


def test_official_source_accepted():
    probe.validate_ubuntu_source(SOURCE)


@pytest.mark.parametrize(
    "source",
    [
        "",
        SOURCE.replace("azure.archive.ubuntu.com", "example.com"),
        SOURCE.replace("ubuntu-archive-keyring.gpg", "other.gpg"),
        SOURCE + "Trusted: yes\n",
        SOURCE + "URIs: http://example.com/ubuntu\n",
        SOURCE.replace("Types: deb", "Types: deb-src"),
    ],
)
def test_nonofficial_or_ambiguous_source_refused(source):
    with pytest.raises(ValueError):
        probe.validate_ubuntu_source(source)


def test_output_allowlist_and_size_limits(tmp_path):
    (tmp_path / "gdb-setup-tail.log").write_bytes(b"x" * 65536)
    (tmp_path / "unrelated-secret.txt").write_bytes(b"x" * 100000)
    probe.check_output_budget(tmp_path)
    (tmp_path / "debugger-probe.json").write_bytes(b"x" * 65537)
    with pytest.raises(ValueError, match="allowlist"):
        probe.check_output_budget(tmp_path)


def test_total_output_cap(tmp_path):
    for i in range(129):
        (tmp_path / f"worker-{i}.fault.tail").write_bytes(b"x" * 65536)
    with pytest.raises(ValueError, match="8 MiB"):
        probe.check_output_budget(tmp_path)


@pytest.mark.skipif(
    probe.sys.platform != "linux", reason="Hosted Bash contract"
)
@pytest.mark.parametrize(
    "plan,allowed",
    [
        ("Inst gdb (15.0 Ubuntu:24.04/noble [amd64])", True),
        ("Inst libc6 [2.39] (2.40 Ubuntu:24.04/noble [amd64])", False),
        ("Remv existing-package [1.0]", False),
    ],
)
def test_setup_plan_refuses_existing_package_changes(tmp_path, plan, allowed):
    import os
    import subprocess
    import yaml

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    setup = next(
        s
        for s in workflow["jobs"]["test"]["steps"]
        if s.get("id") == "gdb_setup"
    )
    script = setup["run"].split("\nlists=", 1)[1].split("\nSETUP", 1)[0]
    script = "lists=" + script
    # The actual shell plan guard runs; package tools are inert functions.
    stubs = """
mktemp() { printf '%s\\n' "$FIXTURE_ROOT"; }
chmod() { :; }
dpkg-query() { :; }
apt-get() {
  case "$*" in
    *--simulate*) printf '%s\\n' "$FIXTURE_PLAN" ;;
    *install*) touch "$FIXTURE_ROOT/installed" ;;
  esac
}
"""
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stubs + script],
        env={
            **os.environ,
            "FIXTURE_ROOT": str(tmp_path),
            "FIXTURE_PLAN": plan,
        },
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode == 0) is allowed
    assert (tmp_path / "installed").exists() is allowed
