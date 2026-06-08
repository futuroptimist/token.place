from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW_DIR = Path(".github/workflows")
RUN_ALL_TESTS_PR_WORKFLOW = "run-all-tests-pr.yml"
PR_REQUIRED_WORKFLOWS = {
    "ci.yml",
    "ci-image.yml",
    "desktop-operator-e2e.yml",
    "desktop-release.yml",
    RUN_ALL_TESTS_PR_WORKFLOW,
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
    assert isinstance(
        on_block, dict
    ), f"{workflow_name} must define a mapping under top-level `on:`"
    return on_block


def _run_all_tests_workflow() -> dict:
    return _load_workflow(WORKFLOW_DIR / RUN_ALL_TESTS_PR_WORKFLOW)


def _job_steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    assert isinstance(steps, list), "workflow job must define steps"
    return [step for step in steps if isinstance(step, dict)]


def _step_runs(step: dict, needle: str) -> bool:
    return needle in str(step.get("run", ""))


def _job_invokes_run_all_tests(job: dict) -> bool:
    return any(_step_runs(step, "./run_all_tests.sh") for step in _job_steps(job))


def _run_all_tests_jobs(workflow_data: dict) -> dict[str, dict]:
    jobs = workflow_data.get("jobs")
    assert isinstance(jobs, dict), "workflow must define jobs"
    return {
        job_id: job
        for job_id, job in jobs.items()
        if isinstance(job, dict) and _job_invokes_run_all_tests(job)
    }


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


def test_image_workflow_keeps_pull_request_validate_only() -> None:
    workflow_data = _load_workflow(WORKFLOW_DIR / "ci-image.yml")
    on_block = _workflow_on_block(workflow_data, "ci-image.yml")

    assert (
        "pull_request" in on_block
    ), "ci-image.yml must validate relay image changes on PRs"
    assert (
        "workflow_dispatch" in on_block
    ), "ci-image.yml should support manual validate-only builds"

    publish_job = workflow_data["jobs"]["publish"]
    assert (
        "github.event_name == 'push'" in publish_job["if"]
    ), "ci-image.yml publish job must stay push-only so PR and workflow_dispatch runs validate only"


def test_canonical_chart_version_is_bumped_for_main_latest_default() -> None:
    chart = yaml.safe_load(
        Path("charts/tokenplace/Chart.yaml").read_text(encoding="utf-8")
    )
    values = yaml.safe_load(
        Path("charts/tokenplace/values.yaml").read_text(encoding="utf-8")
    )

    assert chart["version"] == "0.1.1"
    assert values["image"]["tag"] == "main-latest"
    assert values["image"]["pullPolicy"] == "Always"


def test_ci_installs_playwright_chromium_with_system_dependencies() -> None:
    workflow_data = _load_workflow(WORKFLOW_DIR / "ci.yml")
    steps = workflow_data["jobs"]["test"]["steps"]
    install_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and str(step.get("name", "")).startswith("Install Playwright browsers")
    ]

    assert (
        install_steps
    ), "ci.yml must install Playwright before browser-based guardrails run"
    assert any(
        "playwright install --with-deps chromium" in str(step.get("run", ""))
        for step in install_steps
    ), (
        "The CI browser install must include Playwright system dependencies so the "
        "relay landing-page real desktop-bridge guardrail does not fail when the "
        "runner image lacks Chromium shared libraries."
    )


def test_run_all_tests_uses_absolute_default_real_e2e_model_path() -> None:
    runner = Path("run_all_tests.sh").read_text(encoding="utf-8")

    assert (
        'TOKENPLACE_REAL_E2E_MODEL_PATH="$PWD/.ci-models/stories15M-q4_0.gguf"'
        in runner
    ), (
        "The default real desktop-bridge guardrail model path must be absolute so "
        "subprocess runtime warm-load checks can resolve it after changing working directories."
    )


def test_pr_run_all_tests_workflow_exists_and_is_manually_runnable() -> None:
    workflow_path = WORKFLOW_DIR / RUN_ALL_TESTS_PR_WORKFLOW
    assert workflow_path.exists(), "PRs need a dedicated visible run_all_tests workflow"

    workflow_data = _run_all_tests_workflow()
    on_block = _workflow_on_block(workflow_data, RUN_ALL_TESTS_PR_WORKFLOW)

    assert "pull_request" in on_block, "run_all_tests PR workflow must run on PRs"
    assert (
        "workflow_dispatch" in on_block
    ), "run_all_tests PR workflow should be manually rerunnable for diagnosis"
    assert workflow_data.get("concurrency", {}).get("cancel-in-progress") == "true", (
        "run_all_tests PR workflow should cancel obsolete PR attempts, especially "
        "to avoid wasting macOS minutes"
    )


def test_pr_run_all_tests_workflow_has_visible_linux_and_macos_jobs() -> None:
    workflow_data = _run_all_tests_workflow()
    jobs = _run_all_tests_jobs(workflow_data)

    linux_jobs = [
        job for job in jobs.values() if str(job.get("runs-on", "")).startswith("ubuntu")
    ]
    macos_jobs = [
        job for job in jobs.values() if str(job.get("runs-on", "")).startswith("macos")
    ]

    assert linux_jobs, "PR run_all_tests workflow must include a Linux job"
    assert macos_jobs, "PR run_all_tests workflow must include a macOS job"
    assert any(
        job.get("name") == "Linux run_all_tests.sh" for job in linux_jobs
    ), "Linux run_all_tests job needs a clear PR checks name"
    assert any(
        job.get("name") == "macOS run_all_tests.sh" for job in macos_jobs
    ), "macOS run_all_tests job needs a clear PR checks name"


def test_pr_run_all_tests_jobs_use_modern_python_node_and_playwright() -> None:
    workflow_data = _run_all_tests_workflow()
    jobs = _run_all_tests_jobs(workflow_data)
    assert jobs, "workflow must have jobs that invoke ./run_all_tests.sh"

    for job_id, job in jobs.items():
        steps = _job_steps(job)
        combined_steps = "\n".join(str(step) for step in steps)
        assert (
            "python-version': '3.12" in combined_steps
            or "python-version': 3.12" in combined_steps
        ), f"{job_id} must use Python 3.12 so PR CI mirrors modern local debugging"
        assert (
            "node-version': '20" in combined_steps
            or "node-version': 20" in combined_steps
        ), f"{job_id} must use Node 20 so PR CI mirrors modern local debugging"
        assert (
            "python -m pip install -r requirements.txt" in combined_steps
        ), f"{job_id} must install Python dependencies before running the suite"
        assert (
            "npm ci" in combined_steps
        ), f"{job_id} must install locked Node dependencies before running the suite"

    linux_job = next(
        job for job in jobs.values() if str(job.get("runs-on", "")).startswith("ubuntu")
    )
    macos_job = next(
        job for job in jobs.values() if str(job.get("runs-on", "")).startswith("macos")
    )
    assert "python -m playwright install --with-deps chromium" in "\n".join(
        str(step) for step in _job_steps(linux_job)
    )
    assert "python -m playwright install chromium" in "\n".join(
        str(step) for step in _job_steps(macos_job)
    )


def test_run_all_tests_jobs_do_not_hide_or_skip_suite_failures() -> None:
    workflow_data = _run_all_tests_workflow()
    jobs = _run_all_tests_jobs(workflow_data)
    assert jobs, "workflow must have jobs that invoke ./run_all_tests.sh"

    forbidden_skip_markers = (
        "continue-on-error",
        "steps.prep.outputs.rc",
        "prepare-pi-cgroups",
        "Stop for reboot",
        "requires reboot; hosted CI cannot reboot and must not skip",
    )
    for job_id, job in jobs.items():
        assert (
            str(job.get("continue-on-error", "false")).lower() != "true"
        ), f"{job_id} must not be marked continue-on-error"
        for step in _job_steps(job):
            assert (
                str(step.get("continue-on-error", "false")).lower() != "true"
            ), f"{job_id} run_all_tests path must not use continue-on-error"
        job_text = "\n".join(str(step) for step in _job_steps(job))
        for marker in forbidden_skip_markers:
            assert (
                marker not in job_text
            ), f"{job_id} must not have a green skip/prep branch around ./run_all_tests.sh"
        run_steps = [
            step for step in _job_steps(job) if _step_runs(step, "./run_all_tests.sh")
        ]
        assert run_steps, f"{job_id} must run ./run_all_tests.sh"
        assert all(
            'exit "$status"' in str(step.get("run", "")) for step in run_steps
        ), f"{job_id} must propagate ./run_all_tests.sh exit status"


def test_run_all_tests_jobs_keep_tiny_gguf_guardrail_absolute_and_visible() -> None:
    workflow_data = _run_all_tests_workflow()
    jobs = _run_all_tests_jobs(workflow_data)
    assert jobs, "workflow must have jobs that invoke ./run_all_tests.sh"

    for job_id, job in jobs.items():
        job_text = "\n".join(str(step) for step in _job_steps(job))
        assert (
            "stories15M-q4_0.gguf" in job_text
        ), f"{job_id} must provision the tiny real GGUF guardrail model"
        assert (
            "${GITHUB_WORKSPACE}/.ci-models/stories15M-q4_0.gguf" in job_text
        ), f"{job_id} must provision the model using an absolute github.workspace path"
        assert (
            "TOKENPLACE_REAL_E2E_MODEL_PATH" in job_text
        ), f"{job_id} must pass the tiny GGUF path into run_all_tests.sh"
        assert (
            "${{ github.workspace }}/.ci-models/stories15M-q4_0.gguf" in job_text
        ), f"{job_id} must derive TOKENPLACE_REAL_E2E_MODEL_PATH from github.workspace"


def test_run_all_tests_jobs_append_failure_summary_and_upload_logs() -> None:
    workflow_data = _run_all_tests_workflow()
    jobs = _run_all_tests_jobs(workflow_data)
    assert jobs, "workflow must have jobs that invoke ./run_all_tests.sh"

    for job_id, job in jobs.items():
        job_text = "\n".join(str(step) for step in _job_steps(job))
        assert (
            "GITHUB_STEP_SUMMARY" in job_text
        ), f"{job_id} must append run_all_tests failure context to the job summary"
        assert (
            "tail -n 80 run-all-tests.log" in job_text
        ), f"{job_id} should include concise log tail in the job summary"
        assert (
            "actions/upload-artifact@v4" in job_text
        ), f"{job_id} should upload run_all_tests logs on failure for PR diagnosis"
