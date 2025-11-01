"""Community helpers for the token.place directory endpoints."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple


MODEL_FEEDBACK_ENV_VAR = "TOKEN_PLACE_MODEL_FEEDBACK_PATH"
DEFAULT_MODEL_FEEDBACK_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "community"
    / "model_feedback.jsonl"
)

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


class ContributionQueueError(RuntimeError):
    """Raised when contribution queue data cannot be processed."""


class ModelFeedbackError(RuntimeError):
    """Raised when community feedback data cannot be processed."""


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


def _normalise_endpoints(endpoints: Any) -> List[Dict[str, str]]:
    """Coerce endpoint payloads into ``{"type", "url"}`` dictionaries."""

    if not isinstance(endpoints, list):
        return []

    normalised: List[Dict[str, str]] = []
    for entry in endpoints:
        if not isinstance(entry, dict):
            continue
        endpoint_type = entry.get("type")
        endpoint_url = entry.get("url")
        if not isinstance(endpoint_type, str) or not endpoint_type.strip():
            continue
        if not isinstance(endpoint_url, str) or not endpoint_url.strip():
            continue
        normalised.append(
            {
                "type": endpoint_type.strip(),
                "url": endpoint_url.strip(),
            }
        )

    return normalised


def _normalise_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise a single provider entry and filter required fields."""

    def _require_str_field(field: str) -> str:
        value = provider.get(field)
        if not isinstance(value, str):
            raise CommunityDirectoryError(
                f"Provider field '{field}' must be a non-empty string"
            )

        trimmed = value.strip()
        if not trimmed:
            raise CommunityDirectoryError(
                f"Provider field '{field}' must be a non-empty string"
            )

        return trimmed

    required_fields = ("id", "name", "region")
    cleaned_required = {field: _require_str_field(field) for field in required_fields}

    status_value = provider.get("status", "unknown")
    if isinstance(status_value, str):
        status = status_value.strip() or "unknown"
    else:
        status = "unknown"

    notes = None
    notes_value = provider.get("notes")
    if isinstance(notes_value, str):
        stripped_note = notes_value.strip()
        if stripped_note:
            notes = stripped_note

    contact_value = provider.get("contact", {})
    contact = contact_value if isinstance(contact_value, dict) else {}

    capabilities_value = provider.get("capabilities", [])
    capabilities = capabilities_value if isinstance(capabilities_value, list) else []

    return {
        **cleaned_required,
        "latency_ms": provider.get("latency_ms"),
        "status": status,
        "contact": contact,
        "capabilities": capabilities,
        "notes": notes,
        "endpoints": _normalise_endpoints(provider.get("endpoints")),
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


def _load_contribution_queue() -> Tuple[Dict[str, Any], ...]:
    """Load raw contribution submissions from the queue file."""

    path = _contribution_queue_path()
    if not path.exists():
        return ()

    entries: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, raw_line in enumerate(handle, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:  # pragma: no cover - defensive guard
                    raise ContributionQueueError(
                        f"Invalid JSON in contribution queue (line {line_no})"
                    ) from exc

                if not isinstance(payload, dict):
                    raise ContributionQueueError(
                        f"Queued contribution must be a JSON object (line {line_no})"
                    )

                entries.append(payload)
    except OSError as exc:  # pragma: no cover - IO errors should surface clearly
        raise ContributionQueueError("Unable to read contribution queue") from exc

    return tuple(entries)


def _model_feedback_path() -> Path:
    """Return the path containing community feedback entries."""

    override = os.getenv(MODEL_FEEDBACK_ENV_VAR)
    if override:
        return Path(override)
    return DEFAULT_MODEL_FEEDBACK_PATH


def _parse_timestamp(timestamp: Any, line_no: int) -> datetime | None:
    """Parse optional ISO-8601 timestamps from feedback entries."""

    if timestamp is None:
        return None
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ModelFeedbackError(
            f"submitted_at must be an ISO-8601 string (line {line_no})"
        )

    value = timestamp.strip()
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ModelFeedbackError(
                f"submitted_at must include timezone information (line {line_no})"
            )
        return parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise ModelFeedbackError(
            f"submitted_at must be an ISO-8601 string (line {line_no})"
        ) from exc


def _format_timestamp(value: datetime | None) -> str | None:
    """Serialise timestamps back to a standardised ISO-8601 string."""

    if value is None:
        return None
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalise_feedback_entry(entry: Dict[str, Any], line_no: int) -> Dict[str, Any]:
    """Validate and normalise a single feedback entry."""

    model_id = entry.get("model_id")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ModelFeedbackError(f"model_id is required (line {line_no})")

    raw_rating = entry.get("rating")
    if not isinstance(raw_rating, (int, float)):
        raise ModelFeedbackError(f"rating must be numeric (line {line_no})")

    rating = float(raw_rating)
    if not (1.0 <= rating <= 5.0):
        raise ModelFeedbackError(
            f"rating must be between 1 and 5 inclusive (line {line_no})"
        )

    weight = entry.get("votes") or entry.get("count") or entry.get("weight") or 1
    if not isinstance(weight, int) or weight < 1:
        raise ModelFeedbackError(f"votes/count must be a positive integer (line {line_no})")

    submitted_at = _parse_timestamp(entry.get("submitted_at"), line_no)

    return {
        "model_id": model_id.strip(),
        "rating": rating,
        "weight": weight,
        "submitted_at": submitted_at,
    }


@lru_cache(maxsize=1)
def _load_feedback_entries() -> Tuple[Dict[str, Any], ...]:
    """Load raw community feedback entries from disk."""

    path = _model_feedback_path()
    if not path.exists():
        return ()

    entries: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_no, raw_line in enumerate(handle, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:  # pragma: no cover - defensive guard
                    raise ModelFeedbackError(
                        f"Invalid JSON in feedback file (line {line_no})"
                    ) from exc
                entries.append(_normalise_feedback_entry(payload, line_no))
    except OSError as exc:  # pragma: no cover - IO errors should surface clearly
        raise ModelFeedbackError("Unable to read community feedback file") from exc

    return tuple(entries)


def invalidate_model_feedback_cache() -> None:
    """Clear cached feedback entries."""

    _load_feedback_entries.cache_clear()


def get_model_leaderboard(limit: int | None = None) -> Dict[str, Any]:
    """Aggregate community ratings into a leaderboard payload."""

    if limit is not None:
        if not isinstance(limit, int) or limit <= 0:
            raise ModelFeedbackError("limit must be a positive integer")

    entries = list(_load_feedback_entries())
    if not entries:
        return {"entries": [], "updated": None}

    aggregates: Dict[str, Dict[str, Any]] = {}
    latest_timestamp: datetime | None = None

    for entry in entries:
        model_id = entry["model_id"]
        weight = entry["weight"]
        rating = entry["rating"]
        submitted_at = entry["submitted_at"]

        bucket = aggregates.setdefault(
            model_id,
            {
                "model_id": model_id,
                "total_rating": 0.0,
                "ratings_count": 0,
                "last_feedback_at": None,
            },
        )

        bucket["total_rating"] += rating * weight
        bucket["ratings_count"] += weight

        if submitted_at is not None:
            if bucket["last_feedback_at"] is None or submitted_at > bucket["last_feedback_at"]:
                bucket["last_feedback_at"] = submitted_at
            if latest_timestamp is None or submitted_at > latest_timestamp:
                latest_timestamp = submitted_at

    leaderboard_entries: List[Dict[str, Any]] = []
    for bucket in aggregates.values():
        average = bucket["total_rating"] / bucket["ratings_count"]
        leaderboard_entries.append(
            {
                "model_id": bucket["model_id"],
                "average_rating": round(average, 2),
                "ratings_count": bucket["ratings_count"],
                "last_feedback_at": _format_timestamp(bucket["last_feedback_at"]),
            }
        )

    leaderboard_entries.sort(
        key=lambda item: (
            -item["average_rating"],
            -item["ratings_count"],
            item["model_id"],
        )
    )

    if limit is not None:
        leaderboard_entries = leaderboard_entries[:limit]

    updated = _format_timestamp(latest_timestamp)

    return {"entries": leaderboard_entries, "updated": updated}


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


def get_contribution_summary() -> Dict[str, Any]:
    """Aggregate contribution submissions for maintainers."""

    entries = list(_load_contribution_queue())
    total_submissions = len(entries)

    if not entries:
        return {
            "object": "community.contribution_summary",
            "total_submissions": 0,
            "regions": [],
            "capability_counts": {},
            "last_submission_at": None,
        }

    regions = sorted(
        {
            entry.get("region").strip()
            for entry in entries
            if isinstance(entry.get("region"), str) and entry.get("region").strip()
        }
    )

    capability_counts: Dict[str, int] = {}
    for entry in entries:
        capabilities = entry.get("capabilities", [])
        if not isinstance(capabilities, list):
            continue
        for capability in capabilities:
            if not isinstance(capability, str) or not capability.strip():
                continue
            key = capability.strip()
            capability_counts[key] = capability_counts.get(key, 0) + 1

    sorted_capabilities = dict(
        sorted(
            capability_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    )

    submission_times = [
        entry.get("submitted_at")
        for entry in entries
        if isinstance(entry.get("submitted_at"), str) and entry.get("submitted_at").strip()
    ]
    last_submission_at = max(submission_times, default=None)

    return {
        "object": "community.contribution_summary",
        "total_submissions": total_submissions,
        "regions": regions,
        "capability_counts": sorted_capabilities,
        "last_submission_at": last_submission_at,
    }
