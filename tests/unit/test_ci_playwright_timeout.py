import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys


HELPER = Path(__file__).parents[2] / "scripts" / "run_with_timeout_retry.py"


def test_retry_kills_term_resistant_privileged_descendant(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        assert shutil.which("sudo"), "Ubuntu CI must exercise the privileged cleanup path"
        subprocess.run(["sudo", "--non-interactive", "true"], check=True)
    lock = tmp_path / "attempt.lock"
    survivor = tmp_path / "survivor.pid"
    attempts = tmp_path / "attempts"
    workload = tmp_path / "workload.py"
    workload.write_text(
        """import fcntl, os, pathlib, signal, sys, time
lock, survivor, attempts = map(pathlib.Path, sys.argv[1:])
count = int(attempts.read_text()) + 1 if attempts.exists() else 1
attempts.write_text(str(count))
handle = lock.open('w')
try:
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(73)
if count == 2:
    raise SystemExit(0)
pid = os.fork()
if pid == 0:
    code = f'''import os, pathlib, signal, time
pathlib.Path({str(survivor)!r}).write_text(str(os.getpid()))
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
while True: time.sleep(1)'''
    if os.geteuid() == 0:
        os.execl(sys.executable, sys.executable, '-c', code)
    os.execlp('sudo', 'sudo', '--non-interactive', sys.executable, '-c', code)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True: time.sleep(1)
""",
        encoding="utf-8",
    )
    command = [
        sys.executable, str(HELPER), "--timeout", "0.3", "--grace", "0.2",
        "--attempts", "2", "--delay", "0", "--", sys.executable,
        str(workload), str(lock), str(survivor), str(attempts),
    ]
    result = subprocess.run(command, check=False, timeout=10)
    assert result.returncode == 0
    assert attempts.read_text() == "2"
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    pid = int(survivor.read_text())
    with __import__("pytest").raises(ProcessLookupError):
        os.kill(pid, 0)
