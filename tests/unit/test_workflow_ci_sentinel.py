from __future__ import annotations

from pathlib import Path

import yaml


WORKFLOW_DIR = Path(".github/workflows")
PR_REQUIRED_WORKFLOWS = {
    "ci.yml",
    "build.yml",
    "desktop-operator-e2e.yml",
    "desktop-release.yml",
}


def _load_workflow(path: Path) -> dict:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(data, dict), f"{path} should parse as a YAML mapping"
    return data


def _workflow_on_block(data: dict, workflow_name: str) -> dict:
    on_block = data.get("on")
    if on_block is None:
        # YAML 1.1 loaders can parse the key `on` as boolean `True`.
        on_block = data.get(True)
    assert isinstance(on_block, dict), f"{workflow_name} must define a mapping under top-level `on:`"
    return on_block


def test_required_workflows_trigger_on_pull_requests() -> None:
    missing = []
    for workflow_name in sorted(PR_REQUIRED_WORKFLOWS):
        workflow_data = _load_workflow(WORKFLOW_DIR / workflow_name)
        on_block = _workflow_on_block(workflow_data, workflow_name)
        if "pull_request" not in on_block:
            missing.append(workflow_name)

    assert not missing, (
        "Critical workflows must trigger on pull_request so CI checks always run. "
        f"Missing pull_request trigger in: {', '.join(missing)}"
    )


def test_build_workflow_keeps_pr_paths_gate_in_sync_with_push_paths() -> None:
    workflow_data = _load_workflow(WORKFLOW_DIR / "build.yml")
    on_block = _workflow_on_block(workflow_data, "build.yml")

    push_paths = set((on_block.get("push") or {}).get("paths") or [])
    pr_paths = set((on_block.get("pull_request") or {}).get("paths") or [])

    assert push_paths, "build.yml push trigger should define changed-file paths"
    assert pr_paths, "build.yml pull_request trigger should define changed-file paths"
    assert push_paths == pr_paths, (
        "build.yml push/pull_request path filters must match so PR checks mirror push behavior"
    )
