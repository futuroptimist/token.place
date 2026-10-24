"""One explicitly opted-in hosted probe; never acquire a debugger or model."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile


PROBE_SUBJECT = "[native-probe-once] Capture backend initialization SIGILL"
INFERIOR = """
import os
import resource
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
with open(os.devnull, 'w') as sink:
    os.dup2(sink.fileno(), 1)
    os.dup2(sink.fileno(), 2)
from llama_cpp import llama_cpp
llama_cpp.llama_backend_init()
"""

# GDB's structured API avoids printing arguments, locals or arbitrary memory.
GDB_SCRIPT = r"""
import gdb
import json
import os
from pathlib import Path

report = {"status": "no_signal", "frames": []}
def exited(event):
    report["inferior_exit"] = getattr(event, "exit_code", None)
gdb.events.exited.connect(exited)
def continued(event):
    pid = gdb.selected_inferior().pid
    if pid:
        text = Path('/proc/%d/stat' % pid).read_text()
        stat = text.rsplit(')', 1)[1].split()
        Path('owned-inferior.json').write_text(json.dumps({
            'pid': pid, 'start': stat[19], 'session': os.getsid(pid),
        }))
gdb.events.cont.connect(continued)
def stopped(event):
    if isinstance(event, gdb.SignalEvent):
        report["signal"] = event.stop_signal
        report["status"] = "other_signal"
gdb.events.stop.connect(stopped)
try:
    gdb.execute("run", to_string=True)
    if report.get("signal") == "SIGILL":
        report["status"] = "SIGILL"
        frame = gdb.newest_frame()
        pc = int(frame.pc())
        report["pc"] = hex(pc)
        inferior = gdb.selected_inferior()
        with open("/proc/%d/maps" % inferior.pid, encoding="utf-8") as stream:
            maps = stream.read(1024 * 1024)
        for line in maps.splitlines():
            fields = line.split(None, 5)
            start, end = (int(v, 16) for v in fields[0].split("-"))
            if start <= pc < end and len(fields) == 6:
                report["module"] = fields[5][:4096]
                offset = pc - start + int(fields[2], 16)
                report["module_file_offset"] = hex(offset)
                break
        report["instructions"] = [
            {"pc": hex(i["addr"]), "asm": i["asm"][:512]}
            for i in frame.architecture().disassemble(pc, count=8)
        ]
        for _ in range(16):
            if frame is None:
                break
            report["frames"].append({
                "pc": hex(int(frame.pc())),
                "name": (frame.name() or "unknown")[:512],
            })
            frame = frame.older()
except Exception as error:
    report["diagnostic_error"] = type(error).__name__
    report["status"] = "debugger_error"
Path("probe-result.json").write_text(json.dumps(report), encoding="utf-8")
if gdb.selected_inferior().pid:
    gdb.execute("set confirm off")
    gdb.execute("kill", to_string=True)
"""


def cleanup_inferior(root: Path, session: int) -> str:
    record = root / "owned-inferior.json"
    if not record.exists():
        return "no_record"
    descriptor = None
    try:
        owned = json.loads(record.read_text(encoding="utf-8"))
        pid = int(owned["pid"])
        if not hasattr(os, "pidfd_open") or not hasattr(
            signal, "pidfd_send_signal"
        ):
            return "cleanup_unverified"
        descriptor = os.pidfd_open(pid)
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if (
            owned["session"] != session
            or os.getsid(pid) != session
            or stat[19] != owned["start"]
            or pid == session
        ):
            return "identity_unverified"
        # This recorded inferior may have a different process group from GDB.
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        return "owned_inferior_signal_sent"
    except (FileNotFoundError, ProcessLookupError):
        return "already_exited"
    except (OSError, ValueError, KeyError, IndexError):
        return "cleanup_unverified"
    finally:
        if descriptor is not None:
            os.close(descriptor)


def run_probe(output: Path) -> None:
    if not (
        sys.platform == "linux"
        and os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("GITHUB_RUN_ATTEMPT") == "1"
        and os.environ.get("TOKEN_PLACE_PROBE_SUBJECT") == PROBE_SUBJECT
    ):
        raise SystemExit("One-shot GitHub-hosted probe opt-in required")
    debugger = shutil.which("gdb")
    output.parent.mkdir(parents=True, exist_ok=True)
    if debugger is None:
        output.write_text(
            '{"status":"debugger_unavailable"}', encoding="utf-8"
        )
        return
    with tempfile.TemporaryDirectory(prefix="native-probe-") as directory:
        root = Path(directory)
        (root / "inferior.py").write_text(INFERIOR, encoding="utf-8")
        (root / "capture.py").write_text(GDB_SCRIPT, encoding="utf-8")
        command = [
            debugger,
            "--batch",
            "--nx",
            "--quiet",
            "-iex",
            "set auto-load off",
            "-iex",
            "set debuginfod enabled off",
            "-iex",
            "set disable-randomization off",
            "-iex",
            "set startup-with-shell off",
            "-iex",
            "set pagination off",
            "-iex",
            "set print frame-arguments none",
            "-ex",
            "source capture.py",
            "--args",
            sys.executable,
            str(root / "inferior.py"),
        ]
        # No credentials or arbitrary parent environment reach this probe.
        env = {
            "PATH": os.defpath,
            "HOME": directory,
            "LANG": "C.UTF-8",
            "DEBUGINFOD_URLS": "",
        }
        cleanup = "not_started"
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                process.wait(timeout=90)
            finally:
                cleanup = cleanup_inferior(root, process.pid)
                # Do not target a recycled group after reaping the debugger.
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=10)
            report = {
                "status": "no_report",
                "debugger_exit": process.returncode,
            }
            path = root / "probe-result.json"
            if path.exists() and path.stat().st_size <= 65536:
                report.update(json.loads(path.read_text(encoding="utf-8")))
        except subprocess.TimeoutExpired:
            report = {"status": "debugger_timeout"}
        except OSError as error:
            report = {
                "status": "debugger_unavailable_or_denied",
                "error": type(error).__name__,
            }
        report["inferior_cleanup"] = cleanup
        output.write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    run_probe(Path(".ci-native-diagnostics/debugger-probe.json"))
