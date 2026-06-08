from __future__ import annotations

import re
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


RUN_ALL_TESTS_PR_WORKFLOW = "run-all-tests-pr.yml"


def _workflow_jobs(workflow_name: str) -> dict:
    workflow_data = _load_workflow(WORKFLOW_DIR / workflow_name)
    jobs = workflow_data.get("jobs")
    assert isinstance(jobs, dict), f"{workflow_name} must define workflow jobs"
    return jobs


def _step_run_text(step: dict) -> str:
    return str(step.get("run", "")) if isinstance(step, dict) else ""


def _step_invokes_run_all_tests(step: dict) -> bool:
    return bool(
        re.search(
            r"^\s*\./run_all_tests\.sh(?:\s|$)", _step_run_text(step), re.MULTILINE
        )
    )


def _job_invokes_run_all_tests(job: dict) -> bool:
    return any(
        _step_invokes_run_all_tests(step)
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )


def _run_all_test_jobs() -> list[tuple[str, str, dict]]:
    jobs = []
    for workflow_path in sorted(WORKFLOW_DIR.glob("*.yml")):
        workflow_data = _load_workflow(workflow_path)
        for job_id, job in workflow_data.get("jobs", {}).items():
            if isinstance(job, dict) and _job_invokes_run_all_tests(job):
                jobs.append((workflow_path.name, str(job_id), job))
    return jobs


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

    workflow_data = _load_workflow(workflow_path)
    on_block = _workflow_on_block(workflow_data, RUN_ALL_TESTS_PR_WORKFLOW)

    assert (
        "pull_request" in on_block
    ), "run_all_tests PR workflow must run on pull_request"
    assert (
        "workflow_dispatch" in on_block
    ), "run_all_tests workflow should allow manual reruns"


def test_pr_run_all_tests_workflow_has_visible_linux_and_macos_jobs() -> None:
    jobs = _workflow_jobs(RUN_ALL_TESTS_PR_WORKFLOW)

    linux_job = jobs.get("linux-run-all-tests")
    macos_job = jobs.get("macos-run-all-tests")

    assert isinstance(
        linux_job, dict
    ), "PR workflow must include a Linux run_all_tests job"
    assert isinstance(
        macos_job, dict
    ), "PR workflow must include a macOS run_all_tests job"

    assert linux_job.get("name") == "Linux run_all_tests.sh"
    assert macos_job.get("name") == "macOS run_all_tests.sh"
    assert linux_job.get("runs-on") == "ubuntu-latest"
    assert macos_job.get("runs-on") == "macos-latest"

    assert _job_invokes_run_all_tests(
        linux_job
    ), "Linux job must invoke ./run_all_tests.sh"
    assert _job_invokes_run_all_tests(
        macos_job
    ), "macOS job must invoke ./run_all_tests.sh"


def test_pr_run_all_tests_macos_uses_python_312_and_node_20() -> None:
    macos_job = _workflow_jobs(RUN_ALL_TESTS_PR_WORKFLOW)["macos-run-all-tests"]
    steps = macos_job["steps"]

    python_setup_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses") == "actions/setup-python@v5"
    ]
    node_setup_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses") == "actions/setup-node@v4"
    ]

    assert any(
        step.get("with", {}).get("python-version") == "3.12"
        for step in python_setup_steps
    ), "macOS run_all_tests job must use Python 3.12"
    assert any(
        step.get("with", {}).get("node-version") == "20" for step in node_setup_steps
    ), "macOS run_all_tests job must use Node 20"


def test_run_all_tests_jobs_do_not_continue_on_error_or_skip_reboot_prep() -> None:
    run_all_jobs = _run_all_test_jobs()
    assert run_all_jobs, "At least one workflow job must invoke ./run_all_tests.sh"

    offenders = []
    skip_offenders = []
    for workflow_name, job_id, job in run_all_jobs:
        if str(job.get("continue-on-error", "")).lower() == "true":
            offenders.append(f"{workflow_name}:{job_id}")
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            if str(step.get("continue-on-error", "")).lower() == "true":
                offenders.append(
                    f"{workflow_name}:{job_id}:{step.get('name', '<unnamed step>')}"
                )
            step_text = "\n".join(
                str(step.get(key, "")) for key in ("name", "if", "run")
            ).lower()
            if "steps.prep.outputs" in step_text or (
                "cgroup" in step_text and "exit 0" in step_text
            ):
                skip_offenders.append(
                    f"{workflow_name}:{job_id}:{step.get('name', '<unnamed step>')}"
                )

    assert (
        not offenders
    ), "run_all_tests jobs must not use continue-on-error: " + ", ".join(offenders)
    assert not skip_offenders, (
        "run_all_tests jobs must fail rather than going green through reboot/cgroup/prep "
        "skip branches: " + ", ".join(skip_offenders)
    )


def test_pr_run_all_tests_failure_summary_and_log_artifacts_are_in_jobs() -> None:
    jobs = _workflow_jobs(RUN_ALL_TESTS_PR_WORKFLOW)

    for job_id in ("linux-run-all-tests", "macos-run-all-tests"):
        job = jobs[job_id]
        steps = job["steps"]
        assert any(
            "GITHUB_STEP_SUMMARY" in _step_run_text(step)
            and "run_all_tests.sh failed" in _step_run_text(step)
            for step in steps
            if isinstance(step, dict)
        ), f"{job_id} must append a concise failure summary to GITHUB_STEP_SUMMARY"
        assert any(
            step.get("uses") == "actions/upload-artifact@v4"
            and "run_all_tests.log" in str(step.get("with", {}).get("path", ""))
            for step in steps
            if isinstance(step, dict)
        ), f"{job_id} must upload the run_all_tests log on failure"


def test_run_all_tests_jobs_use_absolute_tiny_gguf_path() -> None:
    run_all_jobs = _run_all_test_jobs()

    missing_absolute_model_path = []
    for workflow_name, job_id, job in run_all_jobs:
        for step in job.get("steps", []):
            if not isinstance(step, dict) or not _step_invokes_run_all_tests(step):
                continue
            env = step.get("env", {})
            model_path = str(env.get("TOKENPLACE_REAL_E2E_MODEL_PATH", ""))
            if "stories15M-q4_0.gguf" not in model_path or not (
                model_path.startswith("${{ github.workspace }}")
                or model_path.startswith("/")
            ):
                missing_absolute_model_path.append(f"{workflow_name}:{job_id}")

    assert not missing_absolute_model_path, (
        "run_all_tests jobs must pass an absolute tiny GGUF guardrail model path "
        "derived from github.workspace: " + ", ".join(missing_absolute_model_path)
    )
