import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from utils.testing.docs_links import find_broken_markdown_links


@pytest.fixture()
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture()
def docs_sandbox(repo_root: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(dir=repo_root) as tmpdir:
        yield Path(tmpdir)


@pytest.mark.unit
def test_markdown_docs_have_no_broken_relative_links(repo_root: Path) -> None:
    docs_root = repo_root / "docs"

    targets = [docs_root, repo_root / "README.md", repo_root / "CONTRIBUTING.md"]
    broken = find_broken_markdown_links(targets)

    assert not broken, f"Found broken documentation links: {broken}"


@pytest.mark.unit
def test_skip_external_and_anchor_links(docs_sandbox: Path) -> None:
    (docs_sandbox / "existing.md").write_text("# heading", encoding="utf-8")
    doc = docs_sandbox / "doc.md"
    doc.write_text(
        """
        [Anchor](#section)
        [External](https://example.com/path)
        [ProtocolRelative](//cdn.example.com/lib.js)
        [Mail](mailto:test@example.com)
        [Javascript](javascript:alert(1))
        [WithTitle](existing.md "Nice title")
        """.strip(),
        encoding="utf-8",
    )

    assert find_broken_markdown_links([docs_sandbox]) == []


@pytest.mark.unit
def test_reports_missing_relative_targets(docs_sandbox: Path, repo_root: Path) -> None:
    doc = docs_sandbox / "broken.md"
    doc.write_text("[Missing](missing.md)", encoding="utf-8")

    assert find_broken_markdown_links([doc]) == [
        (doc.relative_to(repo_root), "missing.md")
    ]


@pytest.mark.unit
def test_ignores_links_outside_repository(docs_sandbox: Path) -> None:
    doc = docs_sandbox / "outside.md"
    doc.write_text("[Outside](../../outside.md)", encoding="utf-8")

    assert find_broken_markdown_links([docs_sandbox]) == []


@pytest.mark.unit
def test_handles_unreadable_markdown_file(
    docs_sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unreadable_doc = docs_sandbox / "unreadable.md"
    unreadable_doc.write_text("[Missing](missing.md)", encoding="utf-8")

    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):  # type: ignore[override]
        if self == unreadable_doc:
            raise OSError("boom")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert find_broken_markdown_links([docs_sandbox]) == []
