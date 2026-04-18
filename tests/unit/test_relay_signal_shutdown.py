"""Regression tests for relay signal-driven shutdown behavior."""

from __future__ import annotations

import signal
import threading
import time

import relay


def test_serve_sigint_handler_does_not_block_on_shutdown(monkeypatch) -> None:
    """SIGINT should dispatch shutdown asynchronously so the handler returns quickly."""

    captured_handler = None
    shutdown_entered = threading.Event()
    allow_shutdown_to_finish = threading.Event()

    class FakeServer:
        def shutdown(self) -> None:
            shutdown_entered.set()
            allow_shutdown_to_finish.wait(timeout=2)

        def serve_forever(self) -> None:
            # Simulate a running server that exits once shutdown starts.
            shutdown_entered.wait(timeout=2)

    class DummyAppContext:
        def push(self) -> None:
            return None

        def pop(self) -> None:
            return None

    monkeypatch.setattr(relay, "make_server", lambda *_args, **_kwargs: FakeServer())
    monkeypatch.setattr(relay.app, "app_context", lambda: DummyAppContext())

    def fake_signal(sig, handler):
        nonlocal captured_handler
        if sig == signal.SIGINT:
            captured_handler = handler
        return handler

    monkeypatch.setattr(relay.signal, "signal", fake_signal)

    serve_thread = threading.Thread(target=relay.serve, args=("127.0.0.1", 0), daemon=True)
    serve_thread.start()

    deadline = time.time() + 1
    while captured_handler is None and time.time() < deadline:
        time.sleep(0.01)

    assert captured_handler is not None

    start = time.perf_counter()
    captured_handler(signal.SIGINT, None)
    duration = time.perf_counter() - start
    assert duration < 0.2
    assert shutdown_entered.wait(timeout=1)

    allow_shutdown_to_finish.set()
    serve_thread.join(timeout=1)
    assert not serve_thread.is_alive()
