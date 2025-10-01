"""Unit tests for the resource monitoring utilities."""
from unittest.mock import MagicMock, patch

import pytest


def test_collect_resource_usage_returns_floats():
    """Resource usage metrics should coerce psutil values into floats."""
    from utils.system import resource_monitor as rm

    fake_memory = MagicMock(percent=62.5)

    with patch.object(rm.psutil, 'cpu_percent', return_value=12), \
         patch.object(rm.psutil, 'virtual_memory', return_value=fake_memory):
        usage = rm.collect_resource_usage()

    assert usage['cpu_percent'] == pytest.approx(12.0)
    assert usage['memory_percent'] == pytest.approx(62.5)
    assert usage['gpu_available'] is False
    assert usage['gpu_count'] == 0
    assert usage['gpu_utilization_percent'] == pytest.approx(0.0)
    assert usage['gpu_memory_percent'] == pytest.approx(0.0)


def test_collect_resource_usage_handles_psutil_errors():
    """When psutil fails, metrics should fall back to zero instead of raising."""
    from utils.system import resource_monitor as rm

    with patch.object(rm.psutil, 'cpu_percent', side_effect=Exception("cpu")), \
         patch.object(rm.psutil, 'virtual_memory', side_effect=Exception("mem")):
        usage = rm.collect_resource_usage()

    assert usage['cpu_percent'] == 0.0
    assert usage['memory_percent'] == 0.0
    assert usage['gpu_available'] is False
    assert usage['gpu_count'] == 0
    assert usage['gpu_utilization_percent'] == pytest.approx(0.0)
    assert usage['gpu_memory_percent'] == pytest.approx(0.0)


def test_collect_resource_usage_reports_gpu_metrics_when_available(monkeypatch):
    """GPU metrics should be reported when NVML is accessible."""
    from utils.system import resource_monitor as rm
    import types

    fake_handles = {0: object(), 1: object()}

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=MagicMock(),
        nvmlDeviceGetCount=MagicMock(return_value=2),
        nvmlDeviceGetHandleByIndex=lambda idx: fake_handles[idx],
        nvmlDeviceGetUtilizationRates=lambda handle: types.SimpleNamespace(
            gpu=40.0 if handle is fake_handles[0] else 70.0
        ),
        nvmlDeviceGetMemoryInfo=lambda handle: types.SimpleNamespace(
            total=1000.0 if handle is fake_handles[0] else 2000.0,
            used=400.0 if handle is fake_handles[0] else 1000.0,
        ),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    fake_memory = MagicMock(percent=50.0)
    monkeypatch.setattr(rm.psutil, 'cpu_percent', lambda interval=None: 20.0)
    monkeypatch.setattr(rm.psutil, 'virtual_memory', lambda: fake_memory)

    usage = rm.collect_resource_usage()

    assert usage['gpu_available'] is True
    assert usage['gpu_count'] == 2
    assert usage['gpu_utilization_percent'] == pytest.approx(55.0)
    assert usage['gpu_memory_percent'] == pytest.approx((400.0 + 1000.0) / (1000.0 + 2000.0) * 100.0)
