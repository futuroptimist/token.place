"""Signal-driven shutdown behavior for the relay server."""

from __future__ import annotations

import signal
import threading

import pytest

import relay


class _FakeServer:
    def __init__(self) -> None:
        self.shutdown_calls = 0
        self.shutdown_thread_ids: list[int] = []
        self.shutdown_event = threading.Event()

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.shutdown_thread_ids.append(threading.get_ident())
        self.shutdown_event.set()


@pytest.fixture()
def _clear_draining_flag():
    relay.DRAINING.clear()
    yield
    relay.DRAINING.clear()


def test_request_relay_shutdown_sets_draining_and_runs_shutdown_async(_clear_draining_flag):
    """SIGINT/SIGTERM should set draining state and call shutdown in another thread."""

    fake_server = _FakeServer()
    shutdown_requested = threading.Event()
    calling_thread_id = threading.get_ident()

    relay._request_relay_shutdown(fake_server, shutdown_requested, signal.SIGINT)

    assert shutdown_requested.is_set() is True
    assert relay.DRAINING.is_set() is True
    assert fake_server.shutdown_event.wait(timeout=1.0) is True
    assert fake_server.shutdown_calls == 1
    assert fake_server.shutdown_thread_ids[0] != calling_thread_id


def test_request_relay_shutdown_is_idempotent(_clear_draining_flag):
    """Duplicate shutdown signals should not trigger repeated server shutdown calls."""

    fake_server = _FakeServer()
    shutdown_requested = threading.Event()

    relay._request_relay_shutdown(fake_server, shutdown_requested, signal.SIGTERM)
    assert fake_server.shutdown_event.wait(timeout=1.0) is True

    relay._request_relay_shutdown(fake_server, shutdown_requested, signal.SIGINT)
    assert fake_server.shutdown_calls == 1
