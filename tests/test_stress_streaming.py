"""Stress tests for streaming encryption helpers."""

import pytest

import encrypt

from utils.testing import (
    StreamEncryptionStressResult,
    run_stream_encryption_stress_test,
)


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


def test_stream_encryption_stress_rejects_non_positive_iteration_counts():
    """The stress helper should guard against invalid iteration values."""

    with pytest.raises(ValueError):
        run_stream_encryption_stress_test(iterations=0)


def test_stream_encryption_stress_rejects_non_positive_chunk_sizes():
    """The stress helper should guard against invalid chunk sizes."""

    with pytest.raises(ValueError):
        run_stream_encryption_stress_test(chunk_size=0)


def test_stream_encryption_stress_detects_payload_mismatch(monkeypatch):
    """A mismatch between plaintext and decrypted payload should fail the run."""

    original_decrypt = encrypt.decrypt_stream_chunk

    def tampering_decrypt(*args, **kwargs):
        plaintext, session = original_decrypt(*args, **kwargs)
        return plaintext + b"!", session

    monkeypatch.setattr(encrypt, "decrypt_stream_chunk", tampering_decrypt)

    with pytest.raises(AssertionError):
        run_stream_encryption_stress_test(iterations=1, chunk_size=32)


def test_stream_encryption_stress_result_properties_handle_zero_cases():
    """Derived metrics should gracefully handle zero-based edge cases."""

    zero_iterations = StreamEncryptionStressResult(
        iterations_requested=10,
        iterations_completed=0,
        elapsed_seconds=1.23,
        max_chunk_bytes=1024,
    )
    assert zero_iterations.average_seconds_per_iteration == 0.0

    zero_elapsed = StreamEncryptionStressResult(
        iterations_requested=5,
        iterations_completed=5,
        elapsed_seconds=0.0,
        max_chunk_bytes=512,
    )
    assert zero_elapsed.operations_per_second == float("inf")
