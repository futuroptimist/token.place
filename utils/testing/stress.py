"""Stress test utilities for token.place encryption pipelines."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import encrypt


@dataclass(slots=True)
class StreamEncryptionStressResult:
    """Summary of a streaming encryption stress run."""

    iterations_requested: int
    iterations_completed: int
    elapsed_seconds: float
    max_chunk_bytes: int

    @property
    def average_seconds_per_iteration(self) -> float:
        """Average runtime per iteration."""
        if self.iterations_completed == 0:
            return 0.0
        return self.elapsed_seconds / self.iterations_completed

    @property
    def operations_per_second(self) -> float:
        """Computed throughput for the stress run."""
        if self.elapsed_seconds == 0:
            return float("inf")
        return self.iterations_completed / self.elapsed_seconds


def run_stream_encryption_stress_test(
    *,
    iterations: int = 128,
    chunk_size: int = 1024,
    associated_data: Optional[bytes] = None,
) -> StreamEncryptionStressResult:
    """Run a lightweight stress test against streaming encryption helpers.

    The helper performs ``iterations`` sequential streaming encrypt/decrypt
    operations using a single negotiated AES session. It verifies that each
    ciphertext round-trips to the original plaintext and tracks aggregate
    runtime metrics to aid callers in validating performance envelopes.

    Args:
        iterations: Number of streaming chunks to encrypt and decrypt.
        chunk_size: Size (in bytes) of each randomly generated payload.
        associated_data: Optional associated data bound to every chunk.

    Returns:
        ``StreamEncryptionStressResult`` describing the run.
    """

    if iterations <= 0:
        raise ValueError("iterations must be a positive integer")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    payload_seed = os.urandom(chunk_size)
    private_key_pem, public_key_pem = encrypt.generate_keys()

    session: Optional[encrypt.StreamSession] = None
    decrypt_session: Optional[encrypt.StreamSession] = None

    completed = 0
    start = time.perf_counter()

    payload = payload_seed

    for _ in range(iterations):
        ciphertext_dict, encrypted_key, session = encrypt.encrypt_stream_chunk(
            payload,
            public_key_pem,
            session=session,
            associated_data=associated_data,
        )
        plaintext, decrypt_session = encrypt.decrypt_stream_chunk(
            ciphertext_dict,
            private_key_pem,
            session=decrypt_session,
            encrypted_key=encrypted_key,
            associated_data=associated_data,
        )

        if plaintext != payload:
            raise AssertionError("Decrypted payload does not match original plaintext")

        completed += 1

    elapsed = time.perf_counter() - start

    return StreamEncryptionStressResult(
        iterations_requested=iterations,
        iterations_completed=completed,
        elapsed_seconds=elapsed,
        max_chunk_bytes=chunk_size,
    )
