"""Regression tests for relay signal-driven shutdown behavior."""

from __future__ import annotations

import signal
import threading
import time

import relay


def test_serve_shutdown_signal_is_non_blocking(monkeypatch) -> None:
    """SIGINT handler should return quickly and shut down the server asynchronously."""

    handlers: dict[int, object] = {}
    shutdown_called = threading.Event()

    class DummyServer:
        def shutdown(self) -> None:
            time.sleep(0.3)
            shutdown_called.set()

        def serve_forever(self) -> None:
            handler = handlers[signal.SIGINT]
            handler(signal.SIGINT, None)

    monkeypatch.setattr(relay, "make_server", lambda *_args, **_kwargs: DummyServer())
    monkeypatch.setattr(relay.signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(relay, "DRAINING", threading.Event())

    start = time.perf_counter()
    relay.serve("127.0.0.1", 5010)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5
    assert relay.DRAINING.is_set()
    assert shutdown_called.wait(timeout=1.0)
