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
