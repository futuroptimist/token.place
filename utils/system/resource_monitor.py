"""Helpers for collecting system resource usage metrics."""
from __future__ import annotations

from typing import Dict

import psutil


def collect_resource_usage() -> Dict[str, float]:
    """Return current CPU and memory utilisation percentages."""

    try:
        cpu_percent_raw = psutil.cpu_percent(interval=None)
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
