"""Helpers for collecting system resource usage metrics."""
from __future__ import annotations

import importlib
import sys
from typing import Dict

import psutil


GpuMetrics = Dict[str, float | int | bool]


def _gpu_metrics_default(*, available: bool = False, count: int = 0) -> GpuMetrics:
    """Return a default GPU metrics payload."""

    return {
        'gpu_available': available,
        'gpu_count': int(count) if count else 0,
        'gpu_utilization_percent': 0.0,
        'gpu_memory_percent': 0.0,
    }


def _import_pynvml():
    """Attempt to import ``pynvml`` without raising if unavailable."""

    try:
        return importlib.import_module('pynvml')
    except Exception:
        return None


def _collect_gpu_metrics() -> GpuMetrics:
    """Collect aggregated GPU utilisation metrics when NVML is available."""

    pynvml = _import_pynvml()
    if pynvml is None:
        return _gpu_metrics_default()

    try:
        pynvml.nvmlInit()
    except Exception:
        return _gpu_metrics_default()

    try:
        try:
            count = int(pynvml.nvmlDeviceGetCount())
        except Exception:
            return _gpu_metrics_default()

        if count <= 0:
            return _gpu_metrics_default()

        total_gpu_util = 0.0
        total_memory_used = 0.0
        total_memory = 0.0
        sampled = 0

        for index in range(count):
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(index)
                utilisation = pynvml.nvmlDeviceGetUtilizationRates(handle)
                gpu_percent = float(getattr(utilisation, 'gpu', 0.0))
                memory_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                memory_used = float(getattr(memory_info, 'used', 0.0))
                memory_total = float(getattr(memory_info, 'total', 0.0))
            except Exception:
                continue

            total_gpu_util += gpu_percent
            total_memory_used += max(memory_used, 0.0)
            total_memory += max(memory_total, 0.0)
            sampled += 1

        if sampled == 0:
            return _gpu_metrics_default(available=True, count=count)

        avg_gpu_util = total_gpu_util / sampled
        memory_percent = (total_memory_used / total_memory * 100.0) if total_memory else 0.0

        return {
            'gpu_available': True,
            'gpu_count': count,
            'gpu_utilization_percent': max(0.0, min(100.0, avg_gpu_util)),
            'gpu_memory_percent': max(0.0, min(100.0, memory_percent)),
        }
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


def _cpu_interval_for_platform(platform: str) -> float | None:
    """Return the psutil sampling interval tuned for the active platform."""

    normalized = platform.lower()
    if normalized.startswith(("win", "cygwin")) or normalized == "darwin":
        return 0.0
    return None


def collect_resource_usage() -> Dict[str, float | int | bool]:
    """Return current CPU, memory, and GPU utilisation metrics."""

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

    gpu_metrics = _collect_gpu_metrics()

    return {
        'cpu_percent': cpu_percent,
        'memory_percent': memory_percent,
        **gpu_metrics,
    }
