"""Typed relay processing outcomes shared by compute-node runtimes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RelayProcessingResult:
    """Immutable, privacy-safe outcome for one relay work item.

    ``bool(result)`` and ``submitted`` intentionally mean only that an encrypted
    response or safe error envelope reached the relay. Callers that need model
    success must check ``inference_succeeded`` explicitly.
    """

    inference_succeeded: bool
    submitted: bool
    safe_error_code: Optional[str] = None
    runtime_healthy: bool = True
    recovery_attempted: bool = False
    recovery_succeeded: bool = False
    submission_allowed: bool = True

    def __bool__(self) -> bool:
        """Compatibility: True means encrypted response/error submission succeeded."""

        return self.submitted

    @classmethod
    def submission_failed(
        cls, *, safe_error_code: Optional[str] = None, runtime_healthy: bool = True
    ) -> "RelayProcessingResult":
        return cls(
            inference_succeeded=False,
            submitted=False,
            safe_error_code=safe_error_code,
            runtime_healthy=runtime_healthy,
            recovery_attempted=False,
            recovery_succeeded=False,
            submission_allowed=True,
        )
