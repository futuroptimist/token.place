"""Utilities for tracking cryptography performance metrics."""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, Dict, Iterable, Optional


@dataclass(frozen=True)
class OperationSample:
    """Represents a single performance measurement."""

    operation: str
    duration_ms: float
    payload_bytes: int


class PerformanceMonitor:
    """Collects lightweight performance samples for encryption operations."""

    def __init__(self, *, enabled: bool = False, max_samples: int = 100) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self._enabled = enabled
        self._max_samples = max_samples
        self._samples: Dict[str, Deque[OperationSample]] = {}
        self._lock = Lock()

    @property
    def is_enabled(self) -> bool:
        """Return ``True`` when monitoring is enabled."""

        return self._enabled

    def configure(self, *, enabled: Optional[bool] = None, max_samples: Optional[int] = None) -> None:
        """Update monitor configuration in a threadsafe manner."""

        with self._lock:
            if enabled is not None:
                self._enabled = bool(enabled)
            if max_samples is not None:
                if max_samples <= 0:
                    raise ValueError("max_samples must be positive")
                if max_samples != self._max_samples:
                    self._max_samples = max_samples
                    self._resize_queues()

    def refresh_from_env(self) -> None:
        """Refresh monitor configuration from environment variables."""

        enabled = os.getenv("TOKEN_PLACE_PERF_MONITOR", "0").lower()
        monitor_enabled = enabled not in {"0", "false", "", "no"}
        max_samples_raw = os.getenv("TOKEN_PLACE_PERF_SAMPLES")
        max_samples: Optional[int] = None
        if max_samples_raw:
            try:
                parsed_max_samples = int(max_samples_raw)
            except ValueError:
                max_samples = None
            else:
                max_samples = parsed_max_samples if parsed_max_samples > 0 else None
        self.configure(enabled=monitor_enabled, max_samples=max_samples)

    def record(self, operation: str, payload_bytes: int, duration_seconds: float) -> None:
        """Record a measurement when monitoring is enabled."""

        if not self._enabled:
            return
        if payload_bytes < 0:
            raise ValueError("payload_bytes must be non-negative")
        if duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")

        sample = OperationSample(
            operation=operation,
            duration_ms=duration_seconds * 1000.0,
            payload_bytes=payload_bytes,
        )
        with self._lock:
            queue = self._get_queue(operation)
            queue.append(sample)

    def clear(self, operation: Optional[str] = None) -> None:
        """Clear recorded samples for an operation or all operations."""

        with self._lock:
            if operation is None:
                self._samples.clear()
            else:
                self._samples.pop(operation, None)

    def summary(self, operation: Optional[str] = None) -> Dict[str, float]:
        """Return aggregate statistics for the requested operation."""

        with self._lock:
            samples = self._iter_samples(operation)
            count = len(samples)
            if count == 0:
                return {
                    "count": 0,
                    "avg_duration_ms": 0.0,
                    "avg_payload_bytes": 0.0,
                    "throughput_bytes_per_sec": 0.0,
                }

            total_duration_ms = sum(sample.duration_ms for sample in samples)
            total_payload_bytes = sum(sample.payload_bytes for sample in samples)
            avg_duration_ms = total_duration_ms / count
            avg_payload_bytes = total_payload_bytes / count
            total_duration_seconds = total_duration_ms / 1000.0
            throughput = (
                total_payload_bytes / total_duration_seconds
                if total_duration_seconds > 0
                else 0.0
            )
            return {
                "count": float(count),
                "avg_duration_ms": avg_duration_ms,
                "avg_payload_bytes": avg_payload_bytes,
                "throughput_bytes_per_sec": throughput,
            }

    def _iter_samples(self, operation: Optional[str]) -> Iterable[OperationSample]:
        if operation is not None:
            queue = self._samples.get(operation)
            if not queue:
                return []
            return list(queue)
        combined: list[OperationSample] = []
        for queue in self._samples.values():
            combined.extend(queue)
        return combined

    def _get_queue(self, operation: str) -> Deque[OperationSample]:
        queue = self._samples.get(operation)
        if queue is None or queue.maxlen != self._max_samples:
            existing = list(queue) if queue is not None else []
            queue = deque(existing, maxlen=self._max_samples)
            self._samples[operation] = queue
        return queue

    def _resize_queues(self) -> None:
        for operation, queue in list(self._samples.items()):
            self._samples[operation] = deque(queue, maxlen=self._max_samples)


def _create_monitor() -> PerformanceMonitor:
    monitor = PerformanceMonitor()
    monitor.refresh_from_env()
    return monitor


encryption_monitor = _create_monitor()


def get_encryption_monitor() -> PerformanceMonitor:
    """Return the global encryption performance monitor."""

    return encryption_monitor
