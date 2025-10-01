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

    assert usage == {'cpu_percent': pytest.approx(12.0), 'memory_percent': pytest.approx(62.5)}


def test_collect_resource_usage_handles_psutil_errors():
    """When psutil fails, metrics should fall back to zero instead of raising."""
    from utils.system import resource_monitor as rm

    with patch.object(rm.psutil, 'cpu_percent', side_effect=Exception("cpu")), \
         patch.object(rm.psutil, 'virtual_memory', side_effect=Exception("mem")):
        usage = rm.collect_resource_usage()

    assert usage == {'cpu_percent': 0.0, 'memory_percent': 0.0}


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
    assert usage == {'cpu_percent': pytest.approx(18.0), 'memory_percent': pytest.approx(73.0)}


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
    assert usage == {'cpu_percent': pytest.approx(27.0), 'memory_percent': pytest.approx(55.0)}
