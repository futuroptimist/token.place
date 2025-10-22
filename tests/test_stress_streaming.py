"""Stress tests for streaming encryption helpers."""

import pytest

from utils.testing import run_stream_encryption_stress_test


@pytest.mark.crypto
@pytest.mark.slow
def test_stream_encryption_stress_handles_high_iteration_volume():
    """Streaming helpers should sustain repeated chunk encryption without errors."""

    result = run_stream_encryption_stress_test(iterations=64, chunk_size=512)

    assert result.iterations_requested == 64
    assert result.iterations_completed == 64
    assert result.max_chunk_bytes == 512
    assert result.elapsed_seconds < 5.0, "Stress run exceeded expected time budget"
    assert result.operations_per_second > 10.0
    assert result.average_seconds_per_iteration < 0.1
