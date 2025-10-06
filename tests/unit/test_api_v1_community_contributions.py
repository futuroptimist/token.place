"""Tests for the community contribution submission endpoint."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from relay import app
@pytest.fixture(name="client")
def client_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Provide a Flask test client with an isolated contribution queue."""

    queue_path = tmp_path / "queue.jsonl"
    monkeypatch.setenv("TOKEN_PLACE_CONTRIBUTION_QUEUE", str(queue_path))

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _load_queue(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_submit_contribution_appends_record(client, tmp_path: Path):
    """A valid submission should be queued and acknowledged."""

    queue_path = Path(os.environ["TOKEN_PLACE_CONTRIBUTION_QUEUE"])
    payload = {
        "operator_name": "Compute Collective",
        "region": "us-west",
        "availability": "weekends",
        "capabilities": ["openai-compatible", "gpu"],
        "contact": {"email": "ops@example.org"},
        "hardware": "2x RTX 4090",
        "notes": "Can scale to 4 nodes with notice",
    }

    response = client.post("/api/v1/community/contributions", json=payload)

    assert response.status_code == 202
    body = response.get_json()
    assert body["status"] == "queued"
    submission_id = body["submission_id"]
    # Validate submission_id is a UUID
    UUID(submission_id)

    queued = _load_queue(queue_path)
    assert len(queued) == 1
    record = queued[0]
    assert record["operator_name"] == payload["operator_name"]
    assert record["region"] == payload["region"]
    assert record["availability"] == payload["availability"]
    assert record["capabilities"] == payload["capabilities"]
    assert record["contact"] == payload["contact"]
    assert record["hardware"] == payload["hardware"]
    assert record["notes"] == payload["notes"]
    assert record["submission_id"] == submission_id
    assert record["submitted_at"].endswith("Z")


@pytest.mark.parametrize(
    "payload, expected_message",
    [
        ({}, "operator_name"),
        (
            {
                "operator_name": "Org",
                "region": "",
                "availability": "always",
                "capabilities": ["gpu"],
                "contact": {"email": "ops@example.org"},
            },
            "region",
        ),
        (
            {
                "operator_name": "Org",
                "region": "us",
                "availability": "",
                "capabilities": ["gpu"],
                "contact": {"email": "ops@example.org"},
            },
            "availability",
        ),
        (
            {
                "operator_name": "Org",
                "region": "us",
                "availability": "always",
                "capabilities": [],
                "contact": {"email": "ops@example.org"},
            },
            "Capabilities must be a non-empty list of strings",
        ),
        (
            {
                "operator_name": "Org",
                "region": "us",
                "availability": "always",
                "capabilities": ["gpu"],
                "contact": {},
            },
            "Contact information must include at least one method",
        ),
        (
            {
                "operator_name": "Org",
                "region": "us",
                "availability": "always",
                "capabilities": ["gpu"],
                "contact": {"twitter": "@org"},
            },
            "Unsupported contact field",
        ),
        (
            {
                "operator_name": "Org",
                "region": "us",
                "availability": "always",
                "capabilities": ["gpu"],
                "contact": {"email": ""},
            },
            "Contact field 'email' must be a non-empty string",
        ),
        (
            {
                "operator_name": "Org",
                "region": "us",
                "availability": "always",
                "capabilities": "gpu",
                "contact": {"email": "ops@example.org"},
            },
            "Capabilities must be a non-empty list of strings",
        ),
        (
            {
                "operator_name": "Org",
                "region": "us",
                "availability": "always",
                "capabilities": [""],
                "contact": {"email": "ops@example.org"},
            },
            "Capabilities must contain non-empty strings",
        ),
        (
            {
                "operator_name": "Org",
                "region": "us",
                "availability": "always",
                "capabilities": ["gpu"],
                "contact": {"email": "ops@example.org"},
                "hardware": 123,
            },
            "hardware must be a string when provided",
        ),
    ],
)
def test_submit_contribution_validation_errors(
    client,
    tmp_path: Path,
    payload: dict[str, object],
    expected_message: str,
):
    """Invalid payloads should return descriptive error messages."""

    response = client.post("/api/v1/community/contributions", json=payload)

    assert response.status_code == 400
    assert expected_message in response.get_json()["error"]["message"]


def test_submit_contribution_rejects_non_object_payload(client):
    """The endpoint should reject JSON payloads that are not objects."""

    response = client.post("/api/v1/community/contributions", json=["not", "an", "object"])

    assert response.status_code == 400
    assert (
        response.get_json()["error"]["message"]
        == "Request body must be a JSON object"
    )
