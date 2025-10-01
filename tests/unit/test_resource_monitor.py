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


def test_collect_gpu_metrics_defaults_when_nvml_missing(monkeypatch):
    """When NVML cannot be imported the GPU metrics should return defaults."""
    from utils.system import resource_monitor as rm

    monkeypatch.setattr(rm, "_import_pynvml", lambda: None, raising=False)

    assert rm._collect_gpu_metrics() == rm._gpu_metrics_default()


def test_collect_gpu_metrics_handles_initialisation_errors(monkeypatch):
    """NVML initialisation failures should not bubble up."""
    from utils.system import resource_monitor as rm
    import types

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(side_effect=RuntimeError("no nvml")),
        nvmlShutdown=MagicMock(),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm._collect_gpu_metrics() == rm._gpu_metrics_default()
    fake_nvml.nvmlShutdown.assert_not_called()


def test_collect_gpu_metrics_marks_availability_when_sampling_fails(monkeypatch):
    """If NVML is present but sampling fails we still report availability."""
    from utils.system import resource_monitor as rm
    import types

    shutdown_called = False

    def fake_shutdown():
        nonlocal shutdown_called
        shutdown_called = True

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=fake_shutdown,
        nvmlDeviceGetCount=MagicMock(return_value=2),
        nvmlDeviceGetHandleByIndex=MagicMock(side_effect=RuntimeError("fail")),
        nvmlDeviceGetUtilizationRates=MagicMock(),
        nvmlDeviceGetMemoryInfo=MagicMock(),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    metrics = rm._collect_gpu_metrics()

    assert metrics == rm._gpu_metrics_default(available=True, count=2)
    assert shutdown_called is True


def test_collect_resource_usage_windows_uses_non_blocking_interval(monkeypatch):
    """Windows platforms should request an immediate CPU sample."""
    from utils.system import resource_monitor as rm

    recorded = {}

    def fake_cpu_percent(*, interval):
        recorded['interval'] = interval
        return 18.0

    fake_memory = MagicMock(percent=73.0)

    monkeypatch.setattr(rm.sys, 'platform', 'win32', raising=False)
    monkeypatch.setattr(rm.psutil, 'cpu_percent', fake_cpu_percent)
    monkeypatch.setattr(rm.psutil, 'virtual_memory', lambda: fake_memory)

    usage = rm.collect_resource_usage()

    assert recorded['interval'] == 0.0
    assert usage['cpu_percent'] == pytest.approx(18.0)
    assert usage['memory_percent'] == pytest.approx(73.0)


def test_collect_resource_usage_linux_keeps_lazy_interval(monkeypatch):
    """Linux platforms continue using the lazy psutil sampling strategy."""
    from utils.system import resource_monitor as rm

    recorded = {}

    def fake_cpu_percent(*, interval):
        recorded['interval'] = interval
        return 27.0

    fake_memory = MagicMock(percent=55.0)

    monkeypatch.setattr(rm.sys, 'platform', 'linux', raising=False)
    monkeypatch.setattr(rm.psutil, 'cpu_percent', fake_cpu_percent)
    monkeypatch.setattr(rm.psutil, 'virtual_memory', lambda: fake_memory)

    usage = rm.collect_resource_usage()

    assert recorded['interval'] is None
    assert usage['cpu_percent'] == pytest.approx(27.0)
    assert usage['memory_percent'] == pytest.approx(55.0)
