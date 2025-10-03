"""Community helpers for the token.place directory endpoints."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

COMMUNITY_DIRECTORY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "community" / "providers.json"
)
CONTRIBUTION_QUEUE_ENV_VAR = "TOKEN_PLACE_CONTRIBUTION_QUEUE"
DEFAULT_CONTRIBUTION_QUEUE_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "community"
    / "contribution_queue.jsonl"
)


class CommunityDirectoryError(RuntimeError):
    """Raised when the community directory payload cannot be parsed."""


class ContributionSubmissionError(RuntimeError):
    """Raised when a community contribution submission is invalid."""


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


def _contribution_queue_path() -> Path:
    """Return the path to the queued contribution sink."""

    override = os.getenv(CONTRIBUTION_QUEUE_ENV_VAR)
    if override:
        return Path(override)
    return DEFAULT_CONTRIBUTION_QUEUE_PATH


def _validate_contact(contact: Dict[str, Any]) -> Dict[str, str]:
    """Validate and sanitise contribution contact details."""

    if not isinstance(contact, dict) or not contact:
        raise ContributionSubmissionError(
            "Contact information must include at least one method"
        )

    allowed_fields = {"email", "matrix", "discord", "website"}
    sanitised: Dict[str, str] = {}
    for key, value in contact.items():
        if key not in allowed_fields:
            raise ContributionSubmissionError(
                f"Unsupported contact field '{key}'"
            )
        if not isinstance(value, str) or not value.strip():
            raise ContributionSubmissionError(
                f"Contact field '{key}' must be a non-empty string"
            )
        sanitised[key] = value.strip()

    return sanitised


def _validate_capabilities(capabilities: Any) -> List[str]:
    """Validate the provided capability list."""

    if not isinstance(capabilities, list) or not capabilities:
        raise ContributionSubmissionError(
            "Capabilities must be a non-empty list of strings"
        )

    sanitised: List[str] = []
    for entry in capabilities:
        if not isinstance(entry, str) or not entry.strip():
            raise ContributionSubmissionError(
                "Capabilities must contain non-empty strings"
            )
        sanitised.append(entry.strip())
    return sanitised


def queue_contribution_submission(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and append a contribution submission to the queue file."""

    operator_name = payload.get("operator_name")
    if not isinstance(operator_name, str) or not operator_name.strip():
        raise ContributionSubmissionError(
            "operator_name must be a non-empty string"
        )

    region = payload.get("region")
    if not isinstance(region, str) or not region.strip():
        raise ContributionSubmissionError("region must be a non-empty string")

    availability = payload.get("availability")
    if not isinstance(availability, str) or not availability.strip():
        raise ContributionSubmissionError(
            "availability must describe when capacity is offered"
        )

    contact = _validate_contact(payload.get("contact", {}))
    capabilities = _validate_capabilities(payload.get("capabilities"))

    record: Dict[str, Any] = {
        "submission_id": str(uuid.uuid4()),
        "operator_name": operator_name.strip(),
        "region": region.strip(),
        "availability": availability.strip(),
        "capabilities": capabilities,
        "contact": contact,
        "submitted_at": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
    }

    optional_fields = {
        "hardware": payload.get("hardware"),
        "notes": payload.get("notes"),
    }
    for key, value in optional_fields.items():
        if value is None:
            continue
        if not isinstance(value, str):
            raise ContributionSubmissionError(
                f"{key} must be a string when provided"
            )
        record[key] = value.strip()

    queue_path = _contribution_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    return record
