"""Community helpers for the token.place directory endpoints."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

COMMUNITY_DIRECTORY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "community" / "providers.json"
)


class CommunityDirectoryError(RuntimeError):
    """Raised when the community directory payload cannot be parsed."""


def _load_raw_directory() -> Dict[str, Any]:
    """Load the raw community directory JSON file.

    Returns a dictionary containing the parsed JSON contents. Missing files
    resolve to an empty directory so the API can still respond gracefully.
    """

    if not COMMUNITY_DIRECTORY_PATH.exists():
        return {"providers": [], "updated": None}

    try:
        return json.loads(COMMUNITY_DIRECTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive guard
        raise CommunityDirectoryError("Invalid community provider directory JSON") from exc


def _normalise_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a single provider entry and filter required fields."""

    required_fields = ("id", "name", "region")
    if not all(provider.get(field) for field in required_fields):
        raise CommunityDirectoryError(
            f"Provider entry missing required fields: {required_fields}"
        )

    return {
        "id": provider["id"],
        "name": provider["name"],
        "region": provider["region"],
        "latency_ms": provider.get("latency_ms"),
        "status": provider.get("status", "unknown"),
        "contact": provider.get("contact", {}),
        "capabilities": provider.get("capabilities", []),
        "notes": provider.get("notes"),
    }


@lru_cache(maxsize=1)
def get_provider_directory() -> Dict[str, Any]:
    """Return the cached community provider directory."""

    raw_directory = _load_raw_directory()
    providers: List[Dict[str, Any]] = []
    for entry in raw_directory.get("providers", []):
        providers.append(_normalise_provider(entry))

    return {
        "providers": providers,
        "updated": raw_directory.get("updated"),
    }


def invalidate_provider_directory_cache() -> None:
    """Clear the cached provider directory.

    Useful for tests that update the directory contents.
    """

    get_provider_directory.cache_clear()
