"""Deterministic regressions for preemptible llama subprocess transport."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from utils.llm.model_manager import ModelManager, _LlamaSubprocessDemultiplexer


def test_silent_stdout_reader_cannot_delay_process_termination():
    """A reader blocked in stdout must not delay the terminate signal."""

    process = subprocess.Popen(
        [sys.executable, "-u", "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name != "nt"),
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        ),
    )
    reader_finished = threading.Event()

    def blocked_reader() -> None:
        assert process.stdout is not None
        for _line in process.stdout:
            pass
        reader_finished.set()

    reader = threading.Thread(target=blocked_reader, daemon=True)
    reader.start()
    manager = object.__new__(ModelManager)
    started = time.monotonic()
    try:
        assert manager._close_llm_proxy(SimpleNamespace(_process=process), terminate_process=True)
        elapsed = time.monotonic() - started
        assert elapsed < 1.5
        assert process.poll() is not None
        assert reader_finished.wait(0.5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def test_persistent_demultiplexer_drops_progress_before_authoritative_result():
    command_id = "c1"
    lines = [
        "TOKEN_PLACE_LLAMA_CPP_JSON:"
        + '{"protocol_version":2,"command_id":"c1","type":"inference_progress","sequence":%d}'
        % sequence
        + "\n"
        for sequence in range(32)
    ]
    lines.append(
        "TOKEN_PLACE_LLAMA_CPP_JSON:"
        + '{"protocol_version":2,"command_id":"c1","status":"ok","result":{"safe":true}}\n'
    )

    class BlockingStdout:
        def __init__(self) -> None:
            self.ready = threading.Event()

        def __iter__(self):
            self.ready.wait(1)
            return iter(lines)

    stdout = BlockingStdout()
    process = SimpleNamespace(stdout=stdout)
    mux = _LlamaSubprocessDemultiplexer(process)
    destination = mux.register(command_id)
    stdout.ready.set()
    terminal = None
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        frame = destination.get(timeout=0.2)
        if frame.get("status") == "ok":
            terminal = frame
            break
    assert terminal == {
        "protocol_version": 2,
        "command_id": command_id,
        "status": "ok",
        "result": {"safe": True},
    }
    mux.join(0.5)
