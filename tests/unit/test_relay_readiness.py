"""Readiness behavior for the relay deployment."""

from __future__ import annotations

import pytest

from relay import DRAINING, app


@pytest.fixture()
def relay_client():
    """Provide a Flask test client with a resolvable GPU host."""

    DRAINING.clear()
    had_gpu_host = "gpu_host" in app.config
    previous_gpu_host = app.config.get("gpu_host")
    app.config["gpu_host"] = None

    with app.test_client() as client:
        yield client

    if had_gpu_host:
        app.config["gpu_host"] = previous_gpu_host
    else:
        app.config.pop("gpu_host", None)
    DRAINING.clear()


def test_healthz_reports_draining_state(relay_client):
    """/healthz should return 503 and Retry-After when the relay is draining."""

    response = relay_client.get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"

    try:
        DRAINING.set()
        draining_response = relay_client.get("/healthz")
    finally:
        DRAINING.clear()

    assert draining_response.status_code == 503
    assert draining_response.headers.get("Retry-After") == "0"

    draining_payload = draining_response.get_json()
    assert draining_payload["status"] == "draining"
    assert draining_payload["details"]["shutdown"] is True
