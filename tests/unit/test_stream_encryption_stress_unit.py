"""Unit-level coverage for the streaming encryption stress harness."""

import pytest

import encrypt

from utils.testing import (
    StreamEncryptionStressResult,
    run_stream_encryption_stress_test,
)


def test_stream_encryption_stress_completes_requested_iterations():
    """The stress helper should iterate with a reusable session and associated data."""

    # Use tiny payloads to keep the unit test fast while still exercising the loop.
    result = run_stream_encryption_stress_test(
        iterations=3,
        chunk_size=16,
        associated_data=b"unit-test",
    )

    assert result.iterations_requested == 3
    assert result.iterations_completed == 3
    assert result.max_chunk_bytes == 16
    assert result.elapsed_seconds >= 0.0
    assert result.operations_per_second > 0.0
    assert result.average_seconds_per_iteration >= 0.0


def test_stream_encryption_stress_rejects_invalid_arguments_unit():
    with pytest.raises(ValueError):
        run_stream_encryption_stress_test(iterations=0)

    with pytest.raises(ValueError):
        run_stream_encryption_stress_test(chunk_size=0)


def test_stream_encryption_stress_detects_tampering_unit(monkeypatch):
    original_decrypt = encrypt.decrypt_stream_chunk

    def tampering_decrypt(*args, **kwargs):
        plaintext, session = original_decrypt(*args, **kwargs)
        return plaintext + b"!", session

    monkeypatch.setattr(encrypt, "decrypt_stream_chunk", tampering_decrypt)

    with pytest.raises(AssertionError):
        run_stream_encryption_stress_test(iterations=1, chunk_size=8)


def test_stream_encryption_stress_result_metric_edges_unit():
    zero_iterations = StreamEncryptionStressResult(
        iterations_requested=5,
        iterations_completed=0,
        elapsed_seconds=2.5,
        max_chunk_bytes=64,
    )
    assert zero_iterations.average_seconds_per_iteration == 0.0

    zero_elapsed = StreamEncryptionStressResult(
        iterations_requested=5,
        iterations_completed=5,
        elapsed_seconds=0.0,
        max_chunk_bytes=64,
    )
    assert zero_elapsed.operations_per_second == float("inf")
