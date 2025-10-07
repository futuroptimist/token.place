"""Tests for the community feedback leaderboard endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.v1 import community
from relay import app


FEEDBACK_ENV_VAR = "TOKEN_PLACE_MODEL_FEEDBACK_PATH"


def _write_feedback_file(path: Path, entries: list[dict[str, object]]) -> None:
    lines = [json.dumps(entry) for entry in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture(name="feedback_file")
def feedback_file_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide an isolated feedback store for leaderboard tests."""

    path = tmp_path / "feedback.jsonl"
    monkeypatch.setenv(FEEDBACK_ENV_VAR, str(path))
    yield path


def _prepare_feedback(entries: list[dict[str, object]], feedback_file: Path) -> None:
    _write_feedback_file(feedback_file, entries)
    community.invalidate_model_feedback_cache()


def test_model_leaderboard_orders_by_rating(feedback_file: Path) -> None:
    """Models with higher community ratings should lead the leaderboard."""

    _prepare_feedback(
        [
            {"model_id": "gpt-4o", "rating": 5, "submitted_at": "2024-08-01T12:00:00Z"},
            {"model_id": "gpt-4o", "rating": 4, "submitted_at": "2024-08-02T12:00:00Z"},
            {"model_id": "mixtral", "rating": 5, "submitted_at": "2024-08-03T12:00:00Z"},
            {"model_id": "mixtral", "rating": 3, "submitted_at": "2024-08-04T12:00:00Z"},
            {"model_id": "llama-3", "rating": 4, "submitted_at": "2024-08-05T12:00:00Z"},
            {"model_id": "llama-3", "rating": 4, "submitted_at": "2024-08-06T12:00:00Z"},
        ],
        feedback_file,
    )

    leaderboard = community.get_model_leaderboard()
    entries = leaderboard["entries"]

    assert [entry["model_id"] for entry in entries] == ["gpt-4o", "llama-3", "mixtral"]
    assert entries[0]["average_rating"] == pytest.approx(4.5)
    assert entries[0]["ratings_count"] == 2
    assert leaderboard["updated"] == "2024-08-06T12:00:00Z"


def test_leaderboard_endpoint_applies_limit(feedback_file: Path) -> None:
    """The HTTP endpoint should honour the limit query parameter."""

    _prepare_feedback(
        [
            {"model_id": "gpt-4o", "rating": 5, "submitted_at": "2024-08-01T12:00:00Z"},
            {"model_id": "sonnet", "rating": 5, "submitted_at": "2024-08-02T12:00:00Z"},
            {"model_id": "mixtral", "rating": 4, "submitted_at": "2024-08-03T12:00:00Z"},
        ],
        feedback_file,
    )

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/api/v1/community/leaderboard?limit=1")

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["model_id"] == "gpt-4o"


def test_leaderboard_endpoint_rejects_non_integer_limit() -> None:
    """The HTTP endpoint should validate non-integer limit query parameters."""

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/api/v1/community/leaderboard?limit=abc")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["message"] == "limit must be a positive integer"
    assert payload["error"]["param"] == "limit"


def test_leaderboard_endpoint_rejects_non_positive_limit() -> None:
    """The HTTP endpoint should reject zero or negative limit values."""

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/api/v1/community/leaderboard?limit=0")

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["message"] == "limit must be a positive integer"
    assert payload["error"]["param"] == "limit"


def test_leaderboard_rejects_naive_timestamps(feedback_file: Path) -> None:
    """Feedback entries must include timezone-aware timestamps."""

    _prepare_feedback(
        [
            {"model_id": "gpt-4o", "rating": 5, "submitted_at": "2024-08-01T12:00:00"},
        ],
        feedback_file,
    )

    with pytest.raises(
        community.ModelFeedbackError,
        match="timezone information",
    ):
        community.get_model_leaderboard()


def test_leaderboard_rejects_blank_timestamps(feedback_file: Path) -> None:
    """Feedback entries with blank timestamps should be rejected."""

    _prepare_feedback(
        [
            {"model_id": "gpt-4o", "rating": 5, "submitted_at": "  "},
        ],
        feedback_file,
    )

    with pytest.raises(
        community.ModelFeedbackError,
        match="ISO-8601",
    ):
        community.get_model_leaderboard()


def test_leaderboard_rejects_malformed_timestamps(feedback_file: Path) -> None:
    """Feedback entries with malformed timestamps should surface errors."""

    _prepare_feedback(
        [
            {"model_id": "gpt-4o", "rating": 5, "submitted_at": "not-a-timestamp"},
        ],
        feedback_file,
    )

    with pytest.raises(
        community.ModelFeedbackError,
        match="ISO-8601",
    ):
        community.get_model_leaderboard()


def test_leaderboard_normalises_timezones(feedback_file: Path) -> None:
    """Timestamps with offsets should be normalised to UTC in the payload."""

    _prepare_feedback(
        [
            {"model_id": "gpt-4o", "rating": 5, "submitted_at": "2024-08-01T14:00:00+02:00"},
            {"model_id": "gpt-4o", "rating": 4, "submitted_at": "2024-08-01T10:00:00-02:00"},
        ],
        feedback_file,
    )

    leaderboard = community.get_model_leaderboard()

    assert leaderboard["entries"][0]["last_feedback_at"] == "2024-08-01T12:00:00Z"
    assert leaderboard["updated"] == "2024-08-01T12:00:00Z"


def test_leaderboard_handles_missing_timestamps(feedback_file: Path) -> None:
    """Entries without timestamps should not break aggregation."""

    _prepare_feedback(
        [
            {"model_id": "gpt-4o", "rating": 5},
        ],
        feedback_file,
    )

    leaderboard = community.get_model_leaderboard()

    assert leaderboard["entries"][0]["last_feedback_at"] is None
    assert leaderboard["updated"] is None


def test_leaderboard_rejects_invalid_limit_type() -> None:
    """Passing a non-integer limit raises a validation error."""

    with pytest.raises(community.ModelFeedbackError, match="positive integer"):
        community.get_model_leaderboard(limit="abc")  # type: ignore[arg-type]


def test_leaderboard_rejects_non_positive_limit() -> None:
    """Passing a non-positive limit raises a validation error."""

    with pytest.raises(community.ModelFeedbackError, match="positive integer"):
        community.get_model_leaderboard(limit=0)


def test_leaderboard_endpoint_handles_model_feedback_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model feedback errors from the aggregator should return HTTP 500."""

    def _raise_model_feedback_error(*_: object, **__: object) -> None:
        raise community.ModelFeedbackError("boom")

    monkeypatch.setattr("api.v1.routes.get_model_leaderboard", _raise_model_feedback_error)

    app.config["TESTING"] = True
    with app.test_client() as client:
        response = client.get("/api/v1/community/leaderboard")

    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["message"] == "boom"
