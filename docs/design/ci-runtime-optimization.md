# CI runtime optimization (K-392)

Status: proposed, documentation only. Inspected 2026-11-01 against
[`3b6a2de38d1cf097b27d2c91c8e3c2aaf315185d`](https://github.com/futuroptimist/token.place/tree/3b6a2de38d1cf097b27d2c91c8e3c2aaf315185d).
The implementation would use separate reviewed PRs. This document authorizes no
workflow execution, publishing, deployment, runner provisioning, or security change.

## Recommendation

First add missing Rust dependency/build caching to desktop validation, cancel
superseded PR-only CI runs, and measure both. Then consolidate the duplicate Linux
suite after proving guardrail equivalence and preserving required check behavior.
Finally introduce conservative job-level change detection for unrelated image and
chart validation. Do not start by caching browsers or weakening package/GPU tests.

The largest sampled PR job was Windows package validation (64m38s), including
24m40s building packages and 25m27s testing upgrades/refusals. These are different
operations, not evidence that one can be removed. The Linux suites also overlap,
but removing duplicate work primarily saves runner time; it need not halve the
PR critical path when the suites already run concurrently.

## Current workflow inventory

Links below point to the files to change in an implementation; the inspection
commit above fixes the baseline. PR means `pull_request`, not privileged
`pull_request_target`. No explicit cross-workflow dependency joins these workflows. The Actions API lists
CodeQL as `disabled_inactivity`; its row describes configured coverage, not recent
execution. Historical/deleted workflow registrations and GitHub-managed review or
dependency workflows are not additional YAML files in this baseline.

| Workflow | Events and selection | Jobs, dependencies and platform coverage |
| --- | --- | --- |
| [ci.yml](../../.github/workflows/ci.yml) | Every PR; pushes to main/master; no paths or concurrency | One Ubuntu job, Python 3.12 / Node 20, 30-minute timeout; full suite plus explicit real-model and encrypted-progress UI checks; coverage upload. |
| [run-all-tests-pr.yml](../../.github/workflows/run-all-tests-pr.yml) | Every PR and manual; cancels superseded runs per workflow/PR/ref | Independent Ubuntu Linux suite (90-minute timeout) and Ubuntu Metal change detector; detector gates macos-26 Python 3.11 embedded Metal bootstrap. Detector matches embedded Python, runtime preparation scripts, root requirements and this workflow; manual runs force Metal. |
| [desktop-operator-e2e.yml](../../.github/workflows/desktop-operator-e2e.yml) | PR and main/master push, path-selected desktop source/config, named harnesses, shared utils, API v1/relay and root requirements; concurrency cancellation | Three independent jobs: Ubuntu Python 3.11 / Node 18 Rust/UI/package checks; Windows hosted structural package checks with Node 18; Ubuntu Python 3.9 inspection. No real Windows GPU coverage. |
| [desktop-linux-package.yml](../../.github/workflows/desktop-linux-package.yml) | PR paths include desktop tree, release workflow, package test and shared runtime; manual; concurrency cancellation | Ubuntu 22.04 x64 CPU runtime, Node 20, Rust, deb build and installed-resource checks. No main push trigger. |
| [desktop-macos-smoke.yml](../../.github/workflows/desktop-macos-smoke.yml) | Selected main desktop paths; Monday 09:37 UTC schedule; manual; concurrency cancellation | macos-26 native no-relay lifecycle, Python 3.11 / Node 18, debug app. Not a PR or GPU inference gate. |
| [desktop-release.yml](../../.github/workflows/desktop-release.yml) | desktop-v* tag push or manual (dry-run option); release/ref concurrency | Build matrix: macos-26 arm64, windows-latest x64, Ubuntu 22.04 x64, Node 20 / Python 3.11, fail-fast false. Publish needs build; disabled Windows NVIDIA self-hosted job also needs build, but publish does not depend on that disabled job. |
| [ci-image.yml](../../.github/workflows/ci-image.yml) | PR to main; main and v* tag push; manual ref/base | Ubuntu amd64 build/smoke/safety/artifact regression job; publish needs it and only runs for main/v* pushes. Publish builds amd64+arm64, qualifies each immutable candidate platform, then promotes tags. Existing GHA BuildKit cache scope `relay-image`. |
| [ci-helm.yml](../../.github/workflows/ci-helm.yml) | PR to main; main push; manual ref/publish | Ubuntu validation/render/tests/package; publisher needs validation, excludes PRs and other repositories, checks chart changes/version/existence before publication. No concurrency. |
| [codeql.yml](../../.github/workflows/codeql.yml) | PR and push main/master; Sunday 00:00 UTC | Independent JavaScript/Python Ubuntu matrix, fail-fast false, autobuild/analyze. Preserve security scan coverage. |
| [ownership-preflight-prototype.yml](../../.github/workflows/ownership-preflight-prototype.yml) | PR/main push limited to experiment tree and workflow | Windows/Ubuntu matrix, fail-fast false, synthetic CMake/CTest fixtures only. |
| [qualify-relay-oci.yml](../../.github/workflows/qualify-relay-oci.yml) | Manual only with explicit immutable identity inputs | Ubuntu/QEMU amd64+arm64 qualification, evidence/cleanup; not ordinary PR work. Leave unchanged. |
| [claude.yml](../../.github/workflows/claude.yml) | Comment/review/issue events with owner mention gates | Two alternative assistant jobs; protected execution environment for one mode. Excluded from optimization and never invoked for this design. |

### Repeated work and existing caches

Both broad Linux jobs independently install root Python requirements, Valkey,
Chromium/system libraries and root npm dependencies, restore/provision the same
checksum-pinned tiny GGUF, and execute [run_all_tests.sh](../../run_all_tests.sh).
Both already use setup-python pip and setup-node npm caches. Those caches do not
replace installation or tests. Root requirements include unpinned test tooling,
so an unchanged requirements hash does not prove an identical resolved environment.

The runner script executes unit/integration/API/relay/security/browser-crypto/JS
checks and UI guardrails; optional E2E, failure recovery and benchmark selections
are controlled by environment flags. The explicit real desktop-bridge check in
`ci.yml` overlaps the script's model-backed check. Its separate encrypted-progress
check uses `RUN_E2E=1` and must not disappear during consolidation. The PR suite's
absolute model-path preflight and captured failure log must also survive.

Desktop operator jobs run targeted Rust tests before full Rust tests, and build
frontend assets before Tauri invokes its configured build command. Verify actual
invocations in logs before removing any pass. Early targeted checks give faster
failure feedback. Linux package and release already use `Swatinem/rust-cache`;
operator and macOS smoke do not. Windows operator and release already cache pinned
embedded runtime downloads; release also caches macOS archives/wheels. Cache
validated inputs, not installed machine state or signed installers.

`run_all_tests.sh` recognizes several Python Playwright layouts and honors
`PLAYWRIGHT_BROWSERS_PATH`. No token.place browser-path mismatch was established.
DSPACE observations are not evidence of a defect here.

## Timing evidence and limits

Read-only REST run/job/step metadata sampled on 2026-11-01: ten completed runs,
chosen from the latest 50 repository runs to cover the latest six PR workflows,
three corresponding main workflows, and scheduled macOS smoke. This is a small,
selected sample, not a distribution or forecast. Latest PR runs use
`57d26884127169cbe6b23443dc83b5310244b42d` (PR #1927), whose Windows and diagnostic
workflow changes differ from the pinned main inventory. Main and scheduled runs
use the pinned baseline. Do not attribute PR timings exactly to main definitions.

Execution below is job `completed_at - started_at`, including setup/cleanup.
Start delay is job `started_at - run.created_at`; for independent first jobs it
bounds scheduling/setup delay, not a precise queue metric. For dependent jobs it
includes upstream execution. All ten runs reported `run_started_at == created_at`,
so that field alone reveals no useful runner queue duration. Skipped jobs and
invalid/negative skipped-job durations are excluded.

| Run / event / outcome | Job execution | Start delay | Notable steps |
| --- | --- | --- | --- |
| [40282870248](https://github.com/futuroptimist/token.place/actions/runs/40282870248), PR, failed | CI 1m42s | 2s | Python install 15s; browser setup 18s; real-model preflight failed after 9s; full suite did not run. Not a fast successful baseline. |
| [40282870258](https://github.com/futuroptimist/token.place/actions/runs/40282870258), PR, passed | Linux 7m28s; detector 9s | 3s; 2s | Linux suite 5m59s; Python 13s; browser setup 17s; Metal skipped. |
| [40282870359](https://github.com/futuroptimist/token.place/actions/runs/40282870359), PR, passed | Linux operator 8m15s; Windows 64m38s; Python 3.9 inspect 30s | 3s; 3s; 2s | Linux targeted Rust 1m43s, full Rust 1m01s. Windows packages 24m40s; upgrade/refusal checks 25m27s; runtime preparation 41s. |
| [40282870331](https://github.com/futuroptimist/token.place/actions/runs/40282870331), PR, passed | Linux package 7m22s | 2s | CPU runtime preparation 4m30s; deb build 1m09s; Rust cache restore 14s. |
| [40282870283](https://github.com/futuroptimist/token.place/actions/runs/40282870283), PR, passed | Image validation 4m47s | 2s | Build 16s; safety gate 41s; recovered/historical artifact tests 3m22s. |
| [40282870364](https://github.com/futuroptimist/token.place/actions/runs/40282870364), PR, passed | Helm validation 31s | 2s | Python dependencies 16s. |
| [40053135126](https://github.com/futuroptimist/token.place/actions/runs/40053135126), main, passed | CI 7m13s | 2s | Suite 5m01s; explicit real-model 21s; encrypted-progress 40s; browser setup 13s. |
| [40053135187](https://github.com/futuroptimist/token.place/actions/runs/40053135187), main, passed | Image validation 4m23s; publication 7m00s | 2s; 4m27s | Publication multiarch build 2m24s; per-platform qualification 4m09s. Dependency wait is not queue time. |
| [40053135151](https://github.com/futuroptimist/token.place/actions/runs/40053135151), main, passed | Helm validation 32s; publish job 15s | 2s; 37s | Publish job success alone does not establish that a chart was pushed. |
| [40303016794](https://github.com/futuroptimist/token.place/actions/runs/40303016794), scheduled, passed | macOS smoke 3m08s | 3s | Debug Tauri build 2m03s. |

No release-matrix, active Metal bootstrap, CodeQL, cold-cache or queue-pressure
baseline is claimed. Run links preserve conclusions; step metadata does not prove
cache hit rates, model correctness or test equivalence. No percentiles or numerical
savings estimates are justified by this sample.

## Ranked implementation proposals

### 1. Cache missing Rust work and cancel superseded PR CI (small scope)

In `desktop-operator-e2e.yml` and `desktop-macos-smoke.yml`, add Rust caching after
toolchain setup, using the existing Linux-package action pattern and workspace
`desktop-tauri/src-tauri -> target`, with binary caching disabled. Separate keys
by runner OS/architecture/image family, Rust version, target triple, profile and
feature set, Cargo.lock, Cargo.toml, build.rs and relevant Rust/config inputs.
Use a versioned namespace (`ci-rust-v1`); increment it after cache incompatibility.
Do not reuse Windows targets on Linux or CPU builds as GPU builds. Cache restore
must always be followed by Cargo compilation/tests. Do not cache tauri-driver
binaries until its version/toolchain compatibility is explicit.

Add PR-only supersession to `ci.yml`, keyed by workflow plus PR number, with
cancellation enabled only for `pull_request`. Use a unique run ID for non-PR groups
so main checks are not canceled incidentally. Retain existing release cancellation
semantics untouched. Validate two rapid PR updates and an unrelated PR; only the
obsolete PR run should cancel. This reduces wasted work, not single-run execution.

### 2. Consolidate the broad Linux suites (medium scope, highest clear overlap)

First extract common installation/provisioning into a local composite action
(e.g. `.github/actions/setup-linux-tests/action.yml`) used by both existing jobs.
Keep their names, triggers and assertions during this first equivalence stage.
This reduces configuration drift but still executes both suites.

Then make `ci.yml` the single PR/main execution owner: incorporate the absolute
model-path preflight, failure artifact and appropriate timeout from the PR job;
retain the explicit encrypted-progress check, cgroup failure behavior, coverage
upload and error reporting. Remove only the redundant real-model invocation after
proving the script's invocation has identical model/environment/failure semantics.
Currently the script can skip when the real llama_cpp runtime is not importable;
the PR job's file preflight alone does not prevent that. Retain the explicit
real-model invocation until an implementation adds a fail-closed runtime probe
and asserts that the guardrail executed. Test missing runtime as well as missing
model; neither may turn the consolidated CI green.
Keep the script itself as the local/full-suite entrypoint.

Remove the PR Linux job only after owners migrate required contexts to the surviving
check and verify historical consumers. In the same implementation PR, migrate
[tests/unit/test_workflow_ci_sentinel.py](../../tests/unit/test_workflow_ci_sentinel.py):
its dedicated-workflow helpers, `linux-run-all-tests` identity/name, direct
`./run_all_tests.sh` invocation, required PR triggers, coverage, model provisioning
and failure-log assertions must follow the surviving execution owner. Preserve
their behavioral guarantees rather than deleting assertions to make CI green;
retain the Metal-specific contracts against their existing workflow. Run
`python -m pytest -q tests/unit/test_workflow_ci_sentinel.py` before and after
consolidation, alongside the full suite and intentional failure cases.
Preserve Metal detection/bootstrap in
`run-all-tests-pr.yml`; preserve manual full-suite access through an explicitly
reviewed replacement trigger or retain its Linux job for manual runs only. A
reusable workflow called twice is not deduplication. Do not create an always-green
legacy alias, cross-workflow artifact trust bridge, or silently waive a check.

### 3. Decouple unrelated chart/image work using fail-closed job selection

Start with `ci-helm.yml`: run a small change detector and stable reporting gate on
every PR; run validation only for chart files, its workflow, the Helm assertion
test, dependency manifests, shared test fixtures/config and checker scripts.
Start conservatively: unknown paths, detector errors, unavailable base history,
renames/deletions and workflow changes must run validation. A narrowly proven
`docs/design/*.md`-only allowlist is safer initially than an incomplete runtime
allowlist. Document explicit “not applicable” output.

Apply the same pattern to `ci-image.yml` only after enumerating Docker context,
Dockerfile COPY inputs, `.dockerignore`, relay imports, requirements, release-safety
scripts and artifact fixtures. Preserve all image qualification for relevant PRs.
Leave main/tag/manual publication and immutable candidate promotion unchanged in
this first stage. Helm main publish/version decisions must remain intact too.
Do not apply broad docs exclusion to workflows where docs participate in builds.

A stable gate must use `always()`, depend on detector and workload, reject failed
or canceled dependencies, and accept skipped workload only when successful
classification explicitly says it is irrelevant. A workflow-level `paths` skip
can leave a required check pending, whereas skipped jobs can report success;
therefore neither absence nor arbitrary skipping is a valid gate implementation.
The branch-protection API was unavailable during inspection; visible ruleset
6166598 contains deletion/non-fast-forward rules, not proof of all merge policy.
Inventory actual required context names and app IDs with a maintainer before
removing/renaming checks or adding conditional workloads. Test fork PRs as well.
See GitHub's [required checks guidance](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/troubleshooting-required-status-checks).

### 4. Smaller setup improvements; defer speculative caches

Add setup-python pip caching with an explicit Python version and focused-manifest
hash to Helm/image test setup, after validating current interpreter compatibility.
Keep `pip install` on every run. This is low risk but only a small part of sampled
execution. Maintain root and desktop npm lockfile scopes separately; keep `npm ci`,
not cached node_modules. Extend dependency hashes when manifests gain includes.

Retain the tiny-model revision/SHA cache and checksum validation on every restore.
Keep existing BuildKit caching; image safety/artifact tests dominate the sampled
PR job, so another Docker cache is not an evidenced priority. Separate BuildKit
scopes by target/platform if measurement shows eviction contention, without
promoting a PR artifact or skipping per-platform safety qualification.

Do not add browser caching by default: sampled setup was 13–18s and Playwright
[advises against it in general](https://playwright.dev/python/docs/ci#caching-browsers).
If future cold-run evidence supports it, key by OS/image/architecture and the
**resolved Python Playwright version**, not only requirements (which permit
version drift), use one explicit browser path for install and tests, install
system dependencies every time, and launch the expected browser revision as a
probe. Never infer compatibility from any Chromium binary merely existing.

Linux CPU runtime download/wheel caching is a later candidate given its 4m30s
sample: first identify compilation versus download cost. Key by pinned runtime
manifest, Python ABI, architecture, CPU backend, preparation-script hash and
requirements; verify checksums and reconstruct the runtime each time. Never cache
a mutable installed runtime, user data, credentials, or signature-bearing release
outputs. No new lockfile or tool-version changes are part of this design PR.

## Cache trust, platform and release boundaries

Caches are expendable acceleration inputs, not acceptance evidence. GitHub's
[cache access rules](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)
allow PRs to read eligible base caches; keep all cache content free of secrets.
Use only ordinary unprivileged PR execution. Do not introduce privileged PR
checkout, broader token permissions or self-hosted execution to improve speed.
For new compiled caches, prefer trusted default-branch writes and PR restores;
never import PR artifacts/caches into release authority via another workflow.
Avoid broad restore prefixes for ABI/backend-sensitive outputs. A miss or corrupt
entry must fall back to clean preparation and still run identical validation.

PRs validate without publishing. Main retains full Linux coverage and current
image/chart decisions. Release tags retain all platform builds, signatures,
provenance, immutable identity checks, platform qualification and promotion order.
The amd64 smoke build and later multiarch candidate are distinct safety stages;
sharing build layers does not justify deleting either gate.

Coordinate GPU work with open [#1555](https://github.com/futuroptimist/token.place/issues/1555)
and [#1608](https://github.com/futuroptimist/token.place/issues/1608), plus the
[desktop parity checklist](../desktop_parity_validation.md). K-392 does not enable
the disabled NVIDIA job, provision runners, claim GPU inference from hosted CPU
smoke, or relax Metal/CUDA fail-closed behavior. Real hardware acceptance and any
future dependency from publication to GPU evidence belong to those existing plans.

## Rollout, measurement and rollback

1. Capture baseline run/job/step JSON with commit, event, attempt, runner image,
   resolved dependency versions, cache hit/miss and executed/pass/fail/skip counts.
   Retain failures separately; never count early failure as an optimization.
2. Implement proposal 1 independently; compare cold and warm runs of the same
   revision and platform. Include restore/save overhead and total runner seconds
   across jobs, alongside elapsed time to all required checks. Do not sum parallel
   jobs and call that user wait time.
3. Implement proposal 2 in setup-only then execution-consolidation stages. Compare
   collected test identities, flags, model validation, coverage and failure signals.
   Deliberately fail the real-model and encrypted-progress cases in controlled
   implementation validation; both must block the surviving check. Preserve logs.
4. Implement proposal 3 one workflow at a time. Validate docs-only, runtime-only,
   chart-only, dependency-only, workflow-only, mixed, deleted/renamed and fork PR
   cases. Missing diff data must run tests or fail; it must never mean docs-only.
   Verify required checks terminate correctly without changing publication behavior.
5. Consider proposal 4 only after fresh timings justify it. Test cache miss, hit,
   stale key and corruption, Python/Node/Rust upgrades and backend separation.

Collect multiple independent repetitions for each selected scenario before
claiming a gain; report sample size, every failure/cancellation, environment drift
and uncertainty. Queue delay, critical-path execution and total runner usage are
separate outcomes. Acceptance requires preserved test/coverage/guardrail behavior,
no new flaky failures or skipped required coverage, and a measured benefit after
cache overhead; there is no promised percentage target.

Rollback each stage by reverting its workflow/action change. Increment cache
namespace or disable restoration to recover from bad cache entries; clean builds
must still work. Restore both Linux jobs and their required-context mapping together
if consolidation fails. Disable conditional selection (run all jobs) if change
classification is wrong. No rollback should alter tags, published artifacts,
production infrastructure, secrets or GPU qualification requirements.
