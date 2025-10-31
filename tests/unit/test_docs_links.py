from pathlib import Path

import pytest

from utils.testing.docs_links import find_broken_markdown_links


@pytest.mark.unit
def test_markdown_docs_have_no_broken_relative_links():
    repo_root = Path(__file__).resolve().parents[2]
    docs_root = repo_root / "docs"

    targets = [docs_root, repo_root / "README.md", repo_root / "CONTRIBUTING.md"]
    broken = find_broken_markdown_links(targets)

    assert not broken, f"Found broken documentation links: {broken}"
