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


def test_collect_gpu_metrics_handles_zero_device_counts(monkeypatch):
    """A zero GPU count should still trigger NVML shutdown and defaults."""
    from utils.system import resource_monitor as rm
    import types

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=MagicMock(),
        nvmlDeviceGetCount=MagicMock(return_value=0),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm._collect_gpu_metrics() == rm._gpu_metrics_default()
    fake_nvml.nvmlShutdown.assert_called_once()


def test_collect_gpu_metrics_handles_count_errors(monkeypatch):
    """Errors when retrieving the GPU count should be swallowed."""
    from utils.system import resource_monitor as rm
    import types

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=MagicMock(),
        nvmlDeviceGetCount=MagicMock(side_effect=RuntimeError("count fail")),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm._collect_gpu_metrics() == rm._gpu_metrics_default()
    fake_nvml.nvmlShutdown.assert_called_once()


def test_collect_gpu_metrics_ignores_shutdown_errors(monkeypatch):
    """NVML shutdown failures should not surface as test errors."""
    from utils.system import resource_monitor as rm
    import types

    shutdown_calls = 0

    def boom_shutdown():
        nonlocal shutdown_calls
        shutdown_calls += 1
        raise RuntimeError("shutdown boom")

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=boom_shutdown,
        nvmlDeviceGetCount=MagicMock(return_value=0),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm._collect_gpu_metrics() == rm._gpu_metrics_default()
    assert shutdown_calls == 1


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


def test_can_allocate_gpu_memory_returns_true_with_sufficient_headroom(monkeypatch):
    """The helper returns True when any GPU has enough free memory."""
    from utils.system import resource_monitor as rm
    import types

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=MagicMock(),
        nvmlDeviceGetCount=MagicMock(return_value=2),
        nvmlDeviceGetHandleByIndex=lambda idx: idx,
        nvmlDeviceGetMemoryInfo=lambda handle: types.SimpleNamespace(
            total=8_000_000_000.0,
            free=6_000_000_000.0 if handle == 0 else 500_000_000.0,
        ),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm.can_allocate_gpu_memory(4_000_000_000, headroom_percent=0.1) is True
    fake_nvml.nvmlShutdown.assert_called_once()


def test_can_allocate_gpu_memory_returns_false_when_all_gpus_full(monkeypatch):
    """When no GPU meets the requirement the helper reports False."""
    from utils.system import resource_monitor as rm
    import types

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=MagicMock(),
        nvmlDeviceGetCount=MagicMock(return_value=1),
        nvmlDeviceGetHandleByIndex=lambda idx: idx,
        nvmlDeviceGetMemoryInfo=lambda handle: types.SimpleNamespace(
            total=8_000_000_000.0,
            free=1_000_000_000.0,
        ),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm.can_allocate_gpu_memory(4_000_000_000, headroom_percent=0.2) is False
    fake_nvml.nvmlShutdown.assert_called_once()


def test_can_allocate_gpu_memory_defaults_to_true_without_nvml(monkeypatch):
    """CPU-only hosts should not be blocked by the GPU guard."""
    from utils.system import resource_monitor as rm

    monkeypatch.setattr(rm, "_import_pynvml", lambda: None, raising=False)

    assert rm.can_allocate_gpu_memory(4_000_000_000) is True


def test_can_allocate_gpu_memory_handles_non_numeric_requirements(monkeypatch):
    """Non-numeric inputs should coerce to zero and allow allocation."""
    from utils.system import resource_monitor as rm

    sentinel = object()
    monkeypatch.setattr(rm, "_import_pynvml", lambda: sentinel, raising=False)

    assert rm.can_allocate_gpu_memory("not-a-number") is True


@pytest.mark.parametrize(
    'input_value, expected',
    [
        (0.1, 1.1),
        ('25', 1.25),
        (-5, 1.0),
        ('not-a-number', 1.0),
    ],
)
def test_gpu_headroom_multiplier_normalizes_inputs(input_value, expected):
    """The headroom helper should coerce to floats and clamp values sensibly."""
    from utils.system import resource_monitor as rm

    assert rm._gpu_headroom_multiplier(input_value) == pytest.approx(expected)


def test_can_allocate_gpu_memory_returns_true_for_non_positive_requirements(monkeypatch):
    """Zero or negative requirements should trivially pass the headroom guard."""
    from utils.system import resource_monitor as rm

    sentinel = object()
    monkeypatch.setattr(rm, "_import_pynvml", lambda: sentinel, raising=False)

    assert rm.can_allocate_gpu_memory(0) is True
    assert rm.can_allocate_gpu_memory(-1024) is True


def test_can_allocate_gpu_memory_handles_nvml_init_errors(monkeypatch):
    """Initialisation failures should fall back to allowing allocation."""
    from utils.system import resource_monitor as rm
    import types

    shutdown_called = False

    def fake_shutdown():
        nonlocal shutdown_called
        shutdown_called = True

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(side_effect=RuntimeError("init boom")),
        nvmlShutdown=fake_shutdown,
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm.can_allocate_gpu_memory(4_000_000_000) is True
    assert shutdown_called is True


def test_can_allocate_gpu_memory_allows_when_count_lookup_fails(monkeypatch):
    """GPU allocation should allow if NVML cannot report the device count."""
    from utils.system import resource_monitor as rm
    import types

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=MagicMock(),
        nvmlDeviceGetCount=MagicMock(side_effect=RuntimeError("no count")),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm.can_allocate_gpu_memory(4_000_000_000) is True
    fake_nvml.nvmlShutdown.assert_called_once()


def test_can_allocate_gpu_memory_allows_when_no_gpus_present(monkeypatch):
    """Returning zero devices should allow allocation and still shutdown NVML."""
    from utils.system import resource_monitor as rm
    import types

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=MagicMock(),
        nvmlDeviceGetCount=MagicMock(return_value=0),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm.can_allocate_gpu_memory(4_000_000_000) is True
    fake_nvml.nvmlShutdown.assert_called_once()


def test_can_allocate_gpu_memory_skips_devices_with_errors(monkeypatch):
    """Devices that raise during sampling should be ignored in the loop."""
    from utils.system import resource_monitor as rm
    import types

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=MagicMock(),
        nvmlDeviceGetCount=MagicMock(return_value=2),
        nvmlDeviceGetHandleByIndex=MagicMock(side_effect=[RuntimeError("bad"), 1]),
        nvmlDeviceGetMemoryInfo=lambda handle: types.SimpleNamespace(
            free=1_000_000_000.0,
            total=2_000_000_000.0,
        ),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm.can_allocate_gpu_memory(4_000_000_000, headroom_percent=0.1) is False
    fake_nvml.nvmlShutdown.assert_called_once()


def test_can_allocate_gpu_memory_ignores_shutdown_errors(monkeypatch):
    """Errors during NVML shutdown should be suppressed in the finally block."""
    from utils.system import resource_monitor as rm
    import types

    shutdown_calls = 0

    def boom_shutdown():
        nonlocal shutdown_calls
        shutdown_calls += 1
        raise RuntimeError("shutdown boom")

    fake_nvml = types.SimpleNamespace(
        nvmlInit=MagicMock(),
        nvmlShutdown=boom_shutdown,
        nvmlDeviceGetCount=MagicMock(return_value=1),
        nvmlDeviceGetHandleByIndex=lambda idx: idx,
        nvmlDeviceGetMemoryInfo=lambda handle: types.SimpleNamespace(
            free=500_000_000.0,
            total=1_000_000_000.0,
        ),
    )

    monkeypatch.setattr(rm, "_import_pynvml", lambda: fake_nvml, raising=False)

    assert rm.can_allocate_gpu_memory(4_000_000_000) is False
    assert shutdown_calls == 1
