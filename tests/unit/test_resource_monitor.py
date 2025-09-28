"""Unit tests for the resource monitoring utilities."""
from unittest.mock import MagicMock, patch

import pytest


def test_collect_resource_usage_returns_floats():
    """Resource usage metrics should coerce psutil values into floats."""
    fake_memory = MagicMock(percent=62.5)

    with patch('utils.system.resource_monitor.psutil.cpu_percent', return_value=12), \
         patch('utils.system.resource_monitor.psutil.virtual_memory', return_value=fake_memory):
        from utils.system.resource_monitor import collect_resource_usage

        usage = collect_resource_usage()

    assert usage == {'cpu_percent': pytest.approx(12.0), 'memory_percent': pytest.approx(62.5)}
