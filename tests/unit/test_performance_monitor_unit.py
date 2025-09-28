"""Unit tests for the performance monitor helpers."""

import pytest

from utils.performance.monitor import PerformanceMonitor


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    """Ensure performance-related env vars do not leak between tests."""

    monkeypatch.delenv("TOKEN_PLACE_PERF_MONITOR", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_PERF_SAMPLES", raising=False)
    yield
    monkeypatch.delenv("TOKEN_PLACE_PERF_MONITOR", raising=False)
    monkeypatch.delenv("TOKEN_PLACE_PERF_SAMPLES", raising=False)


def test_init_requires_positive_max_samples():
    with pytest.raises(ValueError):
        PerformanceMonitor(max_samples=0)


def test_record_is_noop_when_disabled():
    monitor = PerformanceMonitor()

    monitor.record("encrypt", payload_bytes=10, duration_seconds=0.1)

    assert monitor.summary("encrypt") == {
        "count": 0,
        "avg_duration_ms": 0.0,
        "avg_payload_bytes": 0.0,
        "throughput_bytes_per_sec": 0.0,
    }


def test_record_validates_inputs():
    monitor = PerformanceMonitor(enabled=True)

    with pytest.raises(ValueError):
        monitor.record("encrypt", payload_bytes=-1, duration_seconds=0.1)

    with pytest.raises(ValueError):
        monitor.record("encrypt", payload_bytes=1, duration_seconds=-0.1)


def test_summary_handles_zero_duration():
    monitor = PerformanceMonitor(enabled=True)
    monitor.record("encrypt", payload_bytes=8, duration_seconds=0.0)

    summary = monitor.summary()

    assert summary["count"] == 1.0
    assert summary["avg_duration_ms"] == 0.0
    assert summary["avg_payload_bytes"] == 8.0
    assert summary["throughput_bytes_per_sec"] == 0.0


def test_configure_updates_enabled_and_resizes_queues():
    monitor = PerformanceMonitor(enabled=True, max_samples=3)
    for index in range(3):
        monitor.record("encrypt", payload_bytes=10 + index, duration_seconds=0.1)

    monitor.configure(enabled=False, max_samples=2)

    assert not monitor.is_enabled
    summary = monitor.summary("encrypt")
    assert summary["count"] == 2.0
    assert summary["avg_payload_bytes"] == pytest.approx((11 + 12) / 2)

    monitor.record("encrypt", payload_bytes=20, duration_seconds=0.1)
    assert monitor.summary("encrypt")["count"] == 2.0


def test_configure_rejects_non_positive_max_samples():
    monitor = PerformanceMonitor()

    with pytest.raises(ValueError):
        monitor.configure(max_samples=0)


def test_clear_specific_operation():
    monitor = PerformanceMonitor(enabled=True)
    monitor.record("encrypt", payload_bytes=5, duration_seconds=0.1)
    monitor.record("decrypt", payload_bytes=6, duration_seconds=0.1)

    monitor.clear("encrypt")

    assert monitor.summary("encrypt")["count"] == 0
    assert monitor.summary("decrypt")["count"] == 1.0

    monitor.clear()
    assert monitor.summary("decrypt")["count"] == 0


def test_refresh_from_env_parses_values(monkeypatch):
    monitor = PerformanceMonitor()

    monkeypatch.setenv("TOKEN_PLACE_PERF_MONITOR", "true")
    monkeypatch.setenv("TOKEN_PLACE_PERF_SAMPLES", "not-a-number")
    monitor.refresh_from_env()

    assert monitor.is_enabled
    assert monitor.summary()["count"] == 0
    assert monitor._max_samples == 100

    monkeypatch.setenv("TOKEN_PLACE_PERF_MONITOR", "0")
    monkeypatch.setenv("TOKEN_PLACE_PERF_SAMPLES", "5")
    monitor.refresh_from_env()

    assert not monitor.is_enabled
    assert monitor._max_samples == 5
