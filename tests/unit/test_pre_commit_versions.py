"""Regression tests for pre-commit hook versions promised in the changelog."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
CHANGELOG = REPO_ROOT / "docs" / "CHANGELOG.md"


EXPECTED_HOOK_VERSIONS = {
    "https://github.com/codespell-project/codespell": "v2.4.1",
    "https://github.com/jendrikseipp/vulture": "v2.14",
}

CHANGELOG_PROMISE = "Update pre-commit hooks: codespell to v2.4.1 and vulture to v2.14"


def _load_pre_commit_config() -> Dict[str, Any]:
    raw_text = PRE_COMMIT_CONFIG.read_text(encoding="utf-8")
    return yaml.safe_load(raw_text)


def _extract_repo_rev(config: Dict[str, Any], repo_url: str) -> str:
    for repo in config.get("repos", []):
        if repo.get("repo") == repo_url:
            rev = repo.get("rev")
            if not isinstance(rev, str):
                raise AssertionError(f"Missing rev for repo {repo_url}")
            return rev
    raise AssertionError(f"Repo {repo_url} not found in pre-commit config")


def test_pre_commit_versions_and_changelog_alignment():
    """Ensure promised hook upgrades are present and no longer listed as pending."""
    config = _load_pre_commit_config()

    for repo_url, expected_rev in EXPECTED_HOOK_VERSIONS.items():
        actual_rev = _extract_repo_rev(config, repo_url)
        assert (
            actual_rev == expected_rev
        ), f"Expected {repo_url} to use {expected_rev}, found {actual_rev}"

    changelog_text = CHANGELOG.read_text(encoding="utf-8")
    assert (
        CHANGELOG_PROMISE not in changelog_text
    ), "Changelog still lists the pre-commit upgrade as unreleased"
