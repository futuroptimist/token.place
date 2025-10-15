from __future__ import annotations

from pathlib import Path


def test_architecture_doc_includes_mermaid_diagram() -> None:
    architecture_doc = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "```mermaid" in architecture_doc, "Architecture overview should embed a Mermaid diagram"
    assert "flowchart" in architecture_doc, "Architecture diagram should describe a flowchart layout"
