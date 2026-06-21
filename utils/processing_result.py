"""Typed, privacy-safe compute-node request processing outcomes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProcessingResult:
    """Outcome of processing a relay request without plaintext payload details.

    ``bool(result)`` intentionally preserves legacy submission semantics: it is
    True when an encrypted response *or* safe error envelope was submitted to the
    relay. Callers that need model-inference success must inspect
    ``inference_succeeded`` instead.
    """

    inference_succeeded: bool
    envelope_submitted: bool
    safe_error_code: Optional[str] = None
    runtime_healthy: bool = True
    runtime_recovery_attempted: bool = False
    runtime_recovery_succeeded: bool = False

    def __bool__(self) -> bool:
        """Legacy boolean compatibility: encrypted envelope submission success."""

        return self.envelope_submitted

    @property
    def error_envelope_submitted(self) -> bool:
        """Return True when a safe error envelope, not assistant output, was submitted."""

        return self.envelope_submitted and not self.inference_succeeded


PROCESSING_NOT_SUBMITTED = ProcessingResult(
    inference_succeeded=False,
    envelope_submitted=False,
    runtime_healthy=False,
)
