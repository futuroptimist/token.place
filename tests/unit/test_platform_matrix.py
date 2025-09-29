"""Tests for the cross-platform test matrix helper."""
from __future__ import annotations

import json
from typing import Iterable

import pytest

from utils.testing.platform_matrix import build_pytest_args, get_platform_matrix


@pytest.fixture(scope="module")
def matrix() -> Iterable[dict[str, object]]:
    """Return the generated platform matrix for reuse in tests."""

    return get_platform_matrix()


def test_matrix_includes_all_supported_operating_systems(matrix: Iterable[dict[str, object]]) -> None:
    """Ensure the matrix exports each supported GitHub Actions runner."""

    os_values = {entry["os"] for entry in matrix}
    assert {"ubuntu-latest", "macos-latest", "windows-latest"}.issubset(os_values)


@pytest.mark.parametrize("target", ["ubuntu-latest", "macos-latest", "windows-latest"])
def test_matrix_entries_expose_metadata(target: str, matrix: Iterable[dict[str, object]]) -> None:
    """Matrix entries must include required metadata for CI runners."""

    entry = next(item for item in matrix if item["os"] == target)
    assert isinstance(entry["python"], str)
    assert isinstance(entry["node"], str)
    assert entry["marker_expression"].startswith("not slow")
    env = entry["env"]
    assert env["TOKEN_PLACE_TARGET_OS"].startswith(target.split("-", 1)[0])


@pytest.mark.parametrize("target", ["ubuntu-latest", "macos-latest"])
def test_build_pytest_args_includes_marker_expression(
    target: str, matrix: Iterable[dict[str, object]]
) -> None:
    """The helper should append marker filters to pytest invocations."""

    entry = next(item for item in matrix if item["os"] == target)
    args = build_pytest_args(entry, base_args=["-m", "crypto"])
    assert args[:2] == ["-m", "crypto and (" + entry["marker_expression"] + ")"]
    # Ensure the command can be serialized for JSON consumption
    json.dumps(args)
