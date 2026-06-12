from __future__ import annotations

import hashlib
import os
import stat
import subprocess
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
RUN_ALL_TESTS_PR_WORKFLOW = WORKFLOW_DIR / "run-all-tests-pr.yml"


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
    assert RUN_ALL_TESTS_PR_WORKFLOW.exists(), (
        "A dedicated PR workflow must expose run_all_tests.sh as a visible "
        "GitHub Actions check."
    )
    return _load_workflow(RUN_ALL_TESTS_PR_WORKFLOW)


def _run_all_tests_jobs() -> dict[str, dict]:
    workflow_data = _run_all_tests_workflow()
    jobs = workflow_data.get("jobs", {})
    assert isinstance(jobs, dict), "run-all-tests-pr.yml must define jobs"
    return {
        job_id: job
        for job_id, job in jobs.items()
        if isinstance(job, dict) and "run_all_tests.sh" in str(job.get("name", ""))
    }


def _job_steps(job: dict) -> list[dict]:
    steps = job.get("steps", [])
    assert isinstance(steps, list), "workflow job steps must be a list"
    return [step for step in steps if isinstance(step, dict)]


def _step_runs(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in _job_steps(job))



def test_tiny_gguf_provisioning_accepts_macos_bsd_sha256sum(
    tmp_path: Path,
) -> None:
    model_bytes = b"tiny gguf sentinel fixture"
    model_sha = hashlib.sha256(model_bytes).hexdigest()
    model_path = tmp_path / "stories15M-q4_0.gguf"
    model_path.write_bytes(model_bytes)

    script_text = Path("scripts/provision-ci-tiny-gguf.sh").read_text(encoding="utf-8")
    script_text = script_text.replace(
        'MODEL_SHA256="6151b1929d7f5aa3385d9ddef3393e55587c0a55de661562322bc51dfda93a04"',
        f'MODEL_SHA256="{model_sha}"',
    )
    script_path = tmp_path / "provision-ci-tiny-gguf.sh"
    script_path.write_text(script_text, encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_sha256sum = fake_bin / "sha256sum"
    fake_sha256sum.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'usage: sha256sum [-bctwz] [files ...]' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_sha256sum.chmod(fake_sha256sum.stat().st_mode | stat.S_IXUSR)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    completed = subprocess.run(
        ["bash", str(script_path), str(model_path)],
        check=True,
        env=env,
        text=True,
        capture_output=True,
    )

    assert "Provisioned tiny GGUF model" in completed.stdout


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


def test_helm_workflow_publish_decision_is_idempotent() -> None:
    workflow_text = (WORKFLOW_DIR / "ci-helm.yml").read_text(encoding="utf-8")
    workflow_data = _load_workflow(WORKFLOW_DIR / "ci-helm.yml")
    on_block = _workflow_on_block(workflow_data, "ci-helm.yml")

    publish_input = on_block["workflow_dispatch"]["inputs"]["publish"]
    assert publish_input["type"] == "boolean"
    assert publish_input["default"] == "false"

    publish_job = workflow_data["jobs"]["publish"]
    publish_runs = _step_runs(publish_job)

    assert "github.event_name != 'pull_request'" in publish_job["if"]
    assert "chart_source_paths=(" in publish_runs
    assert '"charts/tokenplace/**"' in publish_runs
    assert "chart_related_paths=(" in publish_runs
    assert '".github/workflows/ci-helm.yml"' in publish_runs
    assert "helm show chart \"${CHART_REF}\" --version \"${CHART_VERSION}\"" in publish_runs
    assert "chart_lookup_status=not_found" in publish_runs
    assert "is_confirmed_missing_chart" in publish_runs
    assert "grep -Eiq '(not found|404|manifest unknown|name unknown)'" not in publish_runs
    assert "chart_lookup_status=error" in publish_runs
    assert "action=fail" in publish_runs
    assert (
        "Publish failed: chart files changed but chart version ${CHART_VERSION} already exists; "
        "bump charts/tokenplace/Chart.yaml version."
    ) in workflow_text
    assert (
        "Publish failed: could not determine whether chart version ${CHART_VERSION} exists"
        in workflow_text
    )
    assert (
        "Publish skipped: chart version ${CHART_VERSION} already exists and no chart files changed."
    ) in workflow_text
    assert "steps.publish_decision.outputs.action == 'publish'" in workflow_text
    assert "helm push \"${package_file}\" \"${chart_registry}\"" in publish_runs
    assert "first-parent fallback" not in publish_runs
    assert (
        "unable to compare full pushed range; treating chart paths as changed"
        in publish_runs
    )
    assert (
        "Publish failed: manual publish requested but chart version ${CHART_VERSION} already exists; "
        "refusing to overwrite immutable OCI artifact."
    ) in workflow_text


def test_helm_chart_lookup_only_confirms_canonical_missing_errors() -> None:
    workflow_data = _load_workflow(WORKFLOW_DIR / "ci-helm.yml")
    publish_runs = _step_runs(workflow_data["jobs"]["publish"])

    assert "is_confirmed_missing_chart()" in publish_runs
    assert (
        'elif is_confirmed_missing_chart "${chart_lookup_log}" "${CHART_VERSION}"; then'
        in publish_runs
    )
    assert "grep -Eiq '(not found|404|manifest unknown|name unknown)'" not in publish_runs
    assert "404" not in publish_runs, (
        "A bare HTTP 404 must not be sufficient to classify chart lookup output "
        "as a confirmed missing chart"
    )
    assert 'grep -Fq ":${chart_version}: not found"' in publish_runs
    assert 'grep -Fq "/manifests/${chart_version} not found:"' in publish_runs
    assert "version[[:space:]]+[^[:space:]]+[[:space:]]+not found" in publish_runs
    assert "chart version[[:space:]]+[^[:space:]]+[[:space:]]+not found" in publish_runs
    assert "manifest unknown" in publish_runs
    assert "name unknown" in publish_runs
    assert "unauthorized" in publish_runs
    assert "forbidden" in publish_runs
    assert "rate limit" in publish_runs
    assert "service unavailable" in publish_runs


def test_helm_chart_lookup_errors_fail_closed_before_push() -> None:
    workflow_text = (WORKFLOW_DIR / "ci-helm.yml").read_text(encoding="utf-8")
    workflow_data = _load_workflow(WORKFLOW_DIR / "ci-helm.yml")
    publish_runs = _step_runs(workflow_data["jobs"]["publish"])

    assert 'if [[ "${chart_lookup_status}" == "error" ]]; then' in publish_runs
    assert (
        "registry lookup must succeed or return a confirmed not-found response before publishing"
        in workflow_text
    )
    error_branch = publish_runs.split('if [[ "${chart_lookup_status}" == "error" ]]; then', 1)[1].split(
        'elif [[ "${EVENT_NAME}" == "push" ]]; then', 1
    )[0]
    assert "action=fail" in error_branch
    assert "action=publish" not in error_branch


def test_helm_manual_publish_existing_chart_fails_instead_of_skipping() -> None:
    workflow_text = (WORKFLOW_DIR / "ci-helm.yml").read_text(encoding="utf-8")
    workflow_data = _load_workflow(WORKFLOW_DIR / "ci-helm.yml")
    publish_runs = _step_runs(workflow_data["jobs"]["publish"])

    assert (
        'elif [[ "${MANUAL_PUBLISH}" == "true" && "${chart_exists}" == "true" ]]; then'
        in publish_runs
    )
    assert (
        "action=fail\n"
        '    reason="Publish failed: manual publish requested but chart version '
        '${CHART_VERSION} already exists; refusing to overwrite immutable OCI artifact."'
        in publish_runs
    ), (
        "Manual publish must fail closed instead of skipping when the immutable chart "
        "already exists"
    )
    assert (
        "Publish skipped: manual dispatch validate/package only; chart version ${CHART_VERSION} already exists."
        in workflow_text
    ), "Manual validate/package-only dispatches must still succeed without publishing"


def test_helm_workflow_checkout_has_history_for_push_diff() -> None:
    workflow_data = _load_workflow(WORKFLOW_DIR / "ci-helm.yml")

    for job_name in ("validate-and-package", "publish"):
        steps = _job_steps(workflow_data["jobs"][job_name])
        checkout_steps = [step for step in steps if step.get("uses") == "actions/checkout@v4"]
        assert checkout_steps, f"{job_name} must check out the repository"
        checkout_with = checkout_steps[0].get("with", {})
        assert checkout_with.get("fetch-depth") == "0", (
            f"{job_name} must fetch enough history for chart publish diff decisions"
        )
        assert checkout_with.get("ref") == (
            "${{ github.event_name == 'workflow_dispatch' && inputs.ref || github.sha }}"
        ), f"{job_name} must pin push checkouts to the triggering commit"


def test_canonical_chart_sets_release_metadata_env_defaults() -> None:
    chart = yaml.safe_load(
        Path("charts/tokenplace/Chart.yaml").read_text(encoding="utf-8")
    )
    values = yaml.safe_load(
        Path("charts/tokenplace/values.yaml").read_text(encoding="utf-8")
    )
    deployment = Path("charts/tokenplace/templates/deployment.yaml").read_text(encoding="utf-8")

    assert chart["appVersion"] == "0.1.1"
    assert "deployEnv" in values
    assert '"TOKENPLACE_RELEASE_VERSION" (dict "name" "TOKENPLACE_RELEASE_VERSION" "value" .Chart.AppVersion)' in deployment
    assert '"TOKENPLACE_CHART_VERSION" (dict "name" "TOKENPLACE_CHART_VERSION" "value" .Chart.Version)' in deployment
    assert '"TOKENPLACE_DEPLOY_ENV" (dict "name" "TOKENPLACE_DEPLOY_ENV" "value" $deployEnv)' in deployment
    assert 'and (not $deployEnv) .Values.ingress.host' in deployment
    assert 'and (not $deployEnv) .Values.ingress.enabled .Values.ingress.host' not in deployment
    assert 'eq .Values.ingress.host "token.place"' in deployment
    assert 'eq .Values.ingress.host "staging.token.place"' in deployment


def test_canonical_relay_image_copies_release_metadata_module() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")

    assert "release_metadata.py /app/" in dockerfile


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


def test_run_all_tests_detects_macos_arm64_playwright_chromium_cache() -> None:
    runner = Path("run_all_tests.sh").read_text(encoding="utf-8")

    assert '"*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"' in runner
    assert '"*/chrome-mac*/headless_shell"' in runner


def test_run_all_tests_uses_absolute_default_real_e2e_model_path() -> None:
    runner = Path("run_all_tests.sh").read_text(encoding="utf-8")

    assert (
        'TOKENPLACE_REAL_E2E_MODEL_PATH="$PWD/.ci-models/stories15M-q4_0.gguf"'
        in runner
    ), (
        "The default real desktop-bridge guardrail model path must be absolute so "
        "subprocess runtime warm-load checks can resolve it after changing working directories."
    )


def test_ci_reboot_or_cgroup_prep_branch_fails_instead_of_green_skip() -> None:
    workflow_data = _load_workflow(WORKFLOW_DIR / "ci.yml")
    steps = workflow_data["jobs"]["test"]["steps"]

    stop_steps = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == "Stop for reboot"
    ]

    assert stop_steps, "ci.yml must make cgroup reboot-needed handling explicit"
    assert "steps.prep.outputs.rc == '2'" in str(stop_steps[0].get("if", ""))
    assert "GITHUB_STEP_SUMMARY" in str(stop_steps[0].get("run", ""))
    assert "exit 1" in str(stop_steps[0].get("run", "")), (
        "A cgroup/reboot preflight branch must fail visibly instead of allowing "
        "the run_all_tests.sh step to be skipped while the PR check stays green."
    )


def test_run_all_tests_pr_workflow_exists_and_triggers_on_prs() -> None:
    workflow_data = _run_all_tests_workflow()
    on_block = _workflow_on_block(workflow_data, "run-all-tests-pr.yml")

    assert "pull_request" in on_block, (
        "run-all-tests-pr.yml must run on pull_request so each PR shows a "
        "visible full-suite check."
    )
    assert (
        "workflow_dispatch" in on_block
    ), "run-all-tests-pr.yml should support manual reruns when diagnosing PR CI."


def test_run_all_tests_pr_workflow_has_visible_linux_and_macos_jobs() -> None:
    jobs = _run_all_tests_jobs()

    assert (
        "linux-run-all-tests" in jobs
    ), "run_all_tests PR workflow must have a Linux job"
    assert (
        "macos-run-all-tests" in jobs
    ), "run_all_tests PR workflow must have a macOS job"
    assert jobs["linux-run-all-tests"]["name"] == "Linux run_all_tests.sh"
    assert jobs["macos-run-all-tests"]["name"] == "macOS run_all_tests.sh"
    assert jobs["linux-run-all-tests"]["runs-on"] == "ubuntu-latest"
    assert jobs["macos-run-all-tests"]["runs-on"] == "macos-latest"


def test_run_all_tests_pr_jobs_invoke_the_full_suite() -> None:
    jobs = _run_all_tests_jobs()

    for job_name in ("linux-run-all-tests", "macos-run-all-tests"):
        job_run = _step_runs(jobs[job_name])
        assert "./run_all_tests.sh" in job_run, (
            f"{job_name} must invoke ./run_all_tests.sh directly so green "
            "means the suite actually ran."
        )
        assert "GITHUB_STEP_SUMMARY" in job_run, (
            f"{job_name} must append run_all_tests pass/fail context to the "
            "job summary for PR diagnosis."
        )


def test_macos_run_all_tests_pr_job_uses_python_312_and_node_20() -> None:
    macos_job = _run_all_tests_jobs()["macos-run-all-tests"]
    steps = _job_steps(macos_job)

    python_steps = [
        step for step in steps if step.get("uses") == "actions/setup-python@v5"
    ]
    node_steps = [step for step in steps if step.get("uses") == "actions/setup-node@v4"]

    assert python_steps, "macOS run_all_tests job must set up Python explicitly"
    assert node_steps, "macOS run_all_tests job must set up Node.js explicitly"
    assert any(
        step.get("with", {}).get("python-version") == "3.12" for step in python_steps
    )
    assert any(step.get("with", {}).get("node-version") == "20" for step in node_steps)


def test_macos_run_all_tests_pr_job_builds_llama_cpp_python_with_metal() -> None:
    macos_job = _run_all_tests_jobs()["macos-run-all-tests"]
    env = macos_job.get("env", {})

    assert env.get("FORCE_CMAKE") == "1"
    cmake_args = str(env.get("CMAKE_ARGS", ""))
    assert "-DGGML_METAL=on" in cmake_args, (
        "macOS Apple Silicon CI must build llama_cpp_python with Metal enabled "
        "so GPU-capable desktop modes do not silently fall back to CPU."
    )
    assert "-DGGML_NATIVE=off" in cmake_args, (
        "macos-latest arm64 runners must disable fragile native CPU feature "
        "probing so dependency validation does not fail before run_all_tests.sh."
    )
    assert "-DCMAKE_OSX_ARCHITECTURES=arm64" in cmake_args
    assert "-DCMAKE_APPLE_SILICON_PROCESSOR=arm64" in cmake_args


def test_run_all_tests_pr_jobs_do_not_continue_on_error() -> None:
    for job_name, job in _run_all_tests_jobs().items():
        assert "continue-on-error" not in job, (
            f"{job_name} must not define continue-on-error at job level; "
            "run_all_tests failures must make the visible PR check red."
        )
        for step in _job_steps(job):
            assert "continue-on-error" not in step, (
                f"{job_name} step {step.get('name', '<unnamed>')} must not define "
                "continue-on-error."
            )


def test_run_all_tests_pr_jobs_have_no_green_reboot_or_cgroup_skip_path() -> None:
    forbidden_green_skip_fragments = (
        "steps.prep.outputs.rc != '2'",
        "steps.prep.outputs.rc == '2'",
        "exit 0",
        "continue-on-error",
        "Skipping run_all_tests",
    )

    for job_name, job in _run_all_tests_jobs().items():
        job_text = yaml.dump(job, sort_keys=True)
        for fragment in forbidden_green_skip_fragments:
            assert fragment not in job_text, (
                f"{job_name} must fail setup/prep problems instead of letting a "
                f"green check skip run_all_tests.sh via {fragment!r}."
            )


def test_run_all_tests_pr_tiny_gguf_path_is_absolute_workspace_path() -> None:
    for job_name, job in _run_all_tests_jobs().items():
        env = job.get("env", {})
        assert (
            env.get("TOKENPLACE_REAL_E2E_MODEL_PATH")
            == "${{ github.workspace }}/.ci-models/stories15M-q4_0.gguf"
        ), (
            f"{job_name} must pass an absolute github.workspace-derived GGUF path "
            "so the real desktop-bridge guardrail cannot be skipped due to cwd changes."
        )
        assert "scripts/provision-ci-tiny-gguf.sh" in _step_runs(
            job
        ), f"{job_name} must provision the tiny real GGUF before run_all_tests.sh."
