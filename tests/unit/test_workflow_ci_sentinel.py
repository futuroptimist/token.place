from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW_DIR = Path(".github/workflows")
PR_REQUIRED_WORKFLOWS = {
    "ci.yml",
    "ci-image.yml",
    "desktop-operator-e2e.yml",
    "desktop-release.yml",
    "run-all-tests-pr.yml",
}
RUN_ALL_TESTS_PR_WORKFLOW = "run-all-tests-pr.yml"


def _load_workflow(path: Path) -> dict:
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(data, dict), f"{path} should parse as a YAML mapping"
    return data


def _step_runs_run_all_tests(step: dict) -> bool:
    return any(
        line.strip().startswith("./run_all_tests.sh")
        for line in str(step.get("run", "")).splitlines()
    )


def _job_invokes_run_all_tests(job: dict) -> bool:
    return any(
        isinstance(step, dict) and _step_runs_run_all_tests(step)
        for step in job.get("steps", [])
    )


def _run_all_tests_pr_workflow() -> dict:
    return _load_workflow(WORKFLOW_DIR / RUN_ALL_TESTS_PR_WORKFLOW)


def _run_all_tests_jobs(workflow_data: dict) -> dict[str, dict]:
    jobs = workflow_data.get("jobs", {})
    assert isinstance(jobs, dict), "run_all_tests PR workflow must define jobs"
    selected = {
        job_id: job
        for job_id, job in jobs.items()
        if isinstance(job, dict) and _job_invokes_run_all_tests(job)
    }
    assert (
        selected
    ), "run_all_tests PR workflow must have jobs that invoke ./run_all_tests.sh"
    return selected


def _workflow_on_block(data: dict, workflow_name: str) -> dict:
    on_block = data.get("on")
    if on_block is None:
        # YAML 1.1 loaders can parse the key `on` as boolean `True`.
        on_block = data.get(True)
    assert isinstance(
        on_block, dict
    ), f"{workflow_name} must define a mapping under top-level `on:`"
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


def test_pr_run_all_tests_workflow_exists_and_triggers_on_pull_requests() -> None:
    workflow_path = WORKFLOW_DIR / RUN_ALL_TESTS_PR_WORKFLOW

    assert (
        workflow_path.exists()
    ), "A dedicated PR-visible run_all_tests workflow must exist"

    workflow_data = _run_all_tests_pr_workflow()
    on_block = _workflow_on_block(workflow_data, RUN_ALL_TESTS_PR_WORKFLOW)

    assert "pull_request" in on_block
    assert "workflow_dispatch" in on_block


def test_pr_run_all_tests_workflow_has_visible_linux_and_macos_jobs() -> None:
    workflow_data = _run_all_tests_pr_workflow()
    jobs = _run_all_tests_jobs(workflow_data)

    linux_jobs = [
        job for job in jobs.values() if str(job.get("runs-on", "")).startswith("ubuntu")
    ]
    macos_jobs = [
        job for job in jobs.values() if str(job.get("runs-on", "")).startswith("macos")
    ]

    assert (
        linux_jobs
    ), "The PR workflow must expose a Linux job that runs ./run_all_tests.sh"
    assert (
        macos_jobs
    ), "The PR workflow must expose a macOS job that runs ./run_all_tests.sh"
    assert any(job.get("name") == "Linux run_all_tests.sh" for job in linux_jobs)
    assert any(job.get("name") == "macOS run_all_tests.sh" for job in macos_jobs)


def _assert_job_uses_python_312_and_node_20(job: dict, label: str) -> None:
    steps = job.get("steps", [])
    python_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses") == "actions/setup-python@v5"
    ]
    node_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses") == "actions/setup-node@v4"
    ]

    assert any(
        step.get("with", {}).get("python-version") == "3.12" for step in python_steps
    ), f"{label} run_all_tests job must use Python 3.12"
    assert any(
        step.get("with", {}).get("node-version") == "20" for step in node_steps
    ), f"{label} run_all_tests job must use Node 20"


def test_pr_run_all_tests_linux_job_uses_python_312_and_node_20() -> None:
    workflow_data = _run_all_tests_pr_workflow()
    jobs = _run_all_tests_jobs(workflow_data)
    linux_jobs = [
        job for job in jobs.values() if str(job.get("runs-on", "")).startswith("ubuntu")
    ]
    assert linux_jobs, "The PR workflow must include a Linux run_all_tests job"

    for job in linux_jobs:
        _assert_job_uses_python_312_and_node_20(job, "Linux")


def test_pr_run_all_tests_macos_job_uses_python_312_and_node_20() -> None:
    workflow_data = _run_all_tests_pr_workflow()
    jobs = _run_all_tests_jobs(workflow_data)
    macos_jobs = [
        job for job in jobs.values() if str(job.get("runs-on", "")).startswith("macos")
    ]
    assert macos_jobs, "The PR workflow must include a macOS run_all_tests job"

    for job in macos_jobs:
        _assert_job_uses_python_312_and_node_20(job, "macOS")


def test_pr_run_all_tests_jobs_do_not_hide_or_skip_suite_failures() -> None:
    workflow_data = _run_all_tests_pr_workflow()

    for job_id, job in _run_all_tests_jobs(workflow_data).items():
        assert (
            job.get("continue-on-error") != "true"
        ), f"{job_id} must fail visibly when ./run_all_tests.sh fails"

        steps = job.get("steps", [])
        run_all_steps = [
            step
            for step in steps
            if isinstance(step, dict) and _step_runs_run_all_tests(step)
        ]
        assert run_all_steps, f"{job_id} must include a ./run_all_tests.sh step"

        for step in run_all_steps:
            assert (
                step.get("continue-on-error") != "true"
            ), f"{job_id} must not mark the ./run_all_tests.sh step continue-on-error"
            run_script = str(step.get("run", ""))
            assert (
                'exit "$status"' in run_script or "exit $status" in run_script
            ), f"{job_id} must propagate the ./run_all_tests.sh exit status"
            assert (
                "GITHUB_STEP_SUMMARY" in run_script
            ), f"{job_id} must append diagnostic context to the job summary"

        combined_runs = "\n".join(
            str(step.get("run", "")) for step in steps if isinstance(step, dict)
        )
        assert "steps.prep.outputs.rc != '2'" not in combined_runs
        assert "steps.prep.outputs.rc == '2'" not in combined_runs
        assert "Stop for reboot" not in combined_runs


def test_pr_run_all_tests_tiny_gguf_guardrail_uses_github_workspace_path() -> None:
    workflow_data = _run_all_tests_pr_workflow()
    env = workflow_data.get("env", {})

    assert (
        env.get("TOKENPLACE_REAL_E2E_MODEL_PATH")
        == "${{ github.workspace }}/.ci-models/stories15M-q4_0.gguf"
    ), (
        "The PR workflow must provide an absolute tiny GGUF path derived from "
        "github.workspace so run_all_tests cannot silently skip the real guardrail."
    )

    for job_id, job in _run_all_tests_jobs(workflow_data).items():
        combined_runs = "\n".join(
            str(step.get("run", ""))
            for step in job.get("steps", [])
            if isinstance(step, dict)
        )
        assert (
            'test -s "$TOKENPLACE_REAL_E2E_MODEL_PATH"' in combined_runs
        ), f"{job_id} must fail preflight if the tiny GGUF was not provisioned"
