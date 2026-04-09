"""Shared schema helpers for distributed API v1 compute payloads.

This module keeps relay, server runtime, and desktop compute-node mode in
lockstep for the migration from local-only inference to distributed compute.
"""

from __future__ import annotations

from typing import Any, Mapping

DISTRIBUTED_V1_REQUIRED_FIELDS = (
    "client_public_key",
    "chat_history",
    "cipherkey",
    "iv",
)


def has_distributed_v1_payload(payload: Mapping[str, Any] | None) -> bool:
    """Return ``True`` when ``payload`` includes the distributed v1 fields."""

    if not isinstance(payload, Mapping):
        return False
    return all(field in payload for field in DISTRIBUTED_V1_REQUIRED_FIELDS)
