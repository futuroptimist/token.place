"""Regression tests for the Docker-based test runner image."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.unit
def test_test_runner_dockerfile_preinstalls_playwright_browsers():
    """Browser tests need Chromium inside the container image."""
    dockerfile_path = Path(__file__).resolve().parents[2] / "docker" / "test-runner.Dockerfile"
    assert dockerfile_path.exists(), "Expected docker/test-runner.Dockerfile to be present"

    dockerfile_text = dockerfile_path.read_text()
    assert "playwright install --with-deps chromium" in dockerfile_text, (
        "Container image should install Playwright Chromium dependencies so browser suites run inside Docker."
    )
