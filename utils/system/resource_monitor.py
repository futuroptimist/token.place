"""Helpers for collecting system resource usage metrics."""
from __future__ import annotations

import sys
from typing import Dict

import psutil


def _cpu_interval_for_platform(platform: str) -> float | None:
    """Return the psutil sampling interval tuned for the active platform."""

    normalized = platform.lower()
    if normalized.startswith(("win", "cygwin")) or normalized == "darwin":
        return 0.0
    return None


def collect_resource_usage() -> Dict[str, float]:
    """Return current CPU and memory utilisation percentages."""

    interval = _cpu_interval_for_platform(sys.platform)

    try:
        cpu_percent_raw = psutil.cpu_percent(interval=interval)
    except Exception:
        cpu_percent_raw = None

    try:
        memory_stats = psutil.virtual_memory()
    except Exception:
        memory_stats = None

    cpu_percent = float(cpu_percent_raw) if cpu_percent_raw is not None else 0.0
    memory_percent = float(getattr(memory_stats, 'percent', 0.0)) if memory_stats else 0.0

    return {
        'cpu_percent': cpu_percent,
        'memory_percent': memory_percent,
    }
