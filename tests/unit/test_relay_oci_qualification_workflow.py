from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import relay_release_safety_gate as gate


WORKFLOW = Path(".github/workflows/qualify-relay-oci.yml")
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
SOURCE_COMMIT = "c" * 40


def workflow() -> dict:
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def script(name: str) -> str:
    steps = workflow()["jobs"]["qualify"]["steps"]
    return next(step["run"] for step in steps if step["name"] == name)


def run(script_text: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script_text], cwd=cwd, env={**os.environ, **env},
        text=True, capture_output=True, check=False,
    )


def descriptor(os_name: str, architecture: str, digest: str, **extra: object) -> dict:
    return {
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "digest": digest, "size": 123, "platform": {"os": os_name, "architecture": architecture},
        **extra,
    }


def index_payload(*manifests: dict) -> dict:
    return {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": list(manifests)}


def index_env(tmp_path: Path, payload: str) -> dict[str, str]:
    (tmp_path / "raw").mkdir()
    (tmp_path / "artifacts").mkdir()
    bindir = tmp_path / "bin"; bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$DOCKER_CALLS\"\nprintf '%s' \"$INDEX_INPUT\"\n", encoding="utf-8"); docker.chmod(0o755)
    return {"PATH": f"{bindir}:{os.environ['PATH']}", "INDEX_INPUT": payload,
        "DOCKER_CALLS": str(tmp_path / "docker-calls"), "RAW_DIR": "raw",
        "ARTIFACT_DIR": "artifacts", "OCI_REPOSITORY": "example.test/relay",
        "INPUT_INDEX_DIGEST": DIGEST_A, "GITHUB_ENV": str(tmp_path / "env")}


def test_manual_permissions_coordinates_and_prohibited_operations() -> None:
    data = workflow(); text = WORKFLOW.read_text(encoding="utf-8")
    assert set(data["on"]) == {"workflow_dispatch"}
    assert data["permissions"] == {"contents": "read", "packages": "read"}
    assert data["jobs"]["qualify"]["env"]["OCI_REPOSITORY"] == "ghcr.io/futuroptimist/tokenplace-relay"
    uses = re.findall(r"(?m)^\s+uses:\s+([^\s#]+)", text)
    assert uses and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses)
    prohibited = r"docker (?:build |push|tag)|imagetools create|kubectl|helm |terraform|gh workflow run|curl |https?://"
    assert not re.search(prohibited, text)
    upload = next(s for s in data["jobs"]["qualify"]["steps"] if s["name"].startswith("Upload"))
    assert upload["if"] == "always()" and len(upload["with"]["path"].splitlines()) == 5


def validation_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    bindir = tmp_path / "bin"; bindir.mkdir()
    git = bindir / "git"
    git.write_text(f"#!/bin/sh\nprintf '%s\\n' {SOURCE_COMMIT}\n", encoding="utf-8"); git.chmod(0o755)
    values = {"INPUT_INDEX_DIGEST": DIGEST_A, "INPUT_SOURCE_COMMIT": SOURCE_COMMIT,
        "INPUT_RELEASE_REF": "refs/tags/v0.1.0", "INPUT_RELEASE_BASE": "main",
        "INPUT_EVIDENCE_LABEL": "step-05b", "RAW_DIR": "raw", "ARTIFACT_DIR": "artifacts",
        "GITHUB_ENV": str(tmp_path / "github-env"), "PATH": f"{bindir}:{os.environ['PATH']}"}
    values.update(overrides)
    return values


def test_input_validation_accepts_valid_bounds_and_gates_registry_steps(tmp_path: Path) -> None:
    env = validation_env(tmp_path, INPUT_RELEASE_REF="r" * 128,
        INPUT_RELEASE_BASE="b" * 128, INPUT_EVIDENCE_LABEL="e" * 63)
    result = run(script("Record gate commit and validate untrusted inputs"), tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "raw/validated-inputs.json").read_text()) == {
        "evidence_label": "e" * 63, "index_digest": DIGEST_A, "release_base": "b" * 128,
        "release_ref": "r" * 128, "source_commit": SOURCE_COMMIT}
    conditional = {step["name"]: step.get("if") for step in workflow()["jobs"]["qualify"]["steps"]}
    assert all(conditional[name] == "steps.inputs.outcome == 'success'" for name in (
        "Set up QEMU for ARM64", "Set up Docker Buildx", "Log in to GHCR for package reads",
        "Inspect and validate the immutable index"))


@pytest.mark.parametrize(("key", "value"), [
    ("INPUT_INDEX_DIGEST", "sha256:" + "g" * 64),
    ("INPUT_SOURCE_COMMIT", "c" * 39),
    ("INPUT_RELEASE_REF", "refs/tags/bad;touch-pwned"),
    ("INPUT_RELEASE_REF", "refs//tags/v1"),
    ("INPUT_RELEASE_BASE", "../main"),
    ("INPUT_RELEASE_BASE", "b" * 129),
    ("INPUT_EVIDENCE_LABEL", "Bad_Label"),
    ("INPUT_EVIDENCE_LABEL", "e" * 64),
])
def test_invalid_inputs_fail_without_a_validated_record(tmp_path: Path, key: str, value: str) -> None:
    result = run(script("Record gate commit and validate untrusted inputs"), tmp_path,
        validation_env(tmp_path, **{key: value}))
    assert result.returncode != 0
    assert not (tmp_path / "raw/validated-inputs.json").exists()
    assert not (tmp_path / "pwned").exists()


def test_valid_index_is_sanitized_and_registry_runs_once(tmp_path: Path) -> None:
    payload = json.dumps(index_payload(descriptor("linux", "amd64", DIGEST_A), descriptor("linux", "arm64", DIGEST_B)))
    result = run(script("Inspect and validate the immutable index"), tmp_path, index_env(tmp_path, payload))
    assert result.returncode == 0, result.stderr
    value = json.loads((tmp_path / "artifacts/index-manifest.json").read_text())
    assert [item["platform"]["architecture"] for item in value["manifests"]] == ["amd64", "arm64"]
    assert (tmp_path / "docker-calls").read_text().splitlines() == [
        f"buildx imagetools inspect --raw example.test/relay@{DIGEST_A}"]
    assert not (tmp_path / "raw/index.raw.json").exists()


@pytest.mark.parametrize("mutate", [
    lambda p: p["manifests"].append(descriptor("linux", "amd64", "sha256:" + "c" * 64)),
    lambda p: p["manifests"].pop(),
    lambda p: p["manifests"].append(descriptor("linux", "s390x", "sha256:" + "c" * 64)),
    lambda p: p["manifests"][0].update(mediaType="application/vnd.oci.image.index.v1+json"),
])
def test_bad_platform_sets_and_nested_indexes_fail(tmp_path: Path, mutate) -> None:
    payload = index_payload(descriptor("linux", "amd64", DIGEST_A), descriptor("linux", "arm64", DIGEST_B)); mutate(payload)
    result = run(script("Inspect and validate the immutable index"), tmp_path, index_env(tmp_path, json.dumps(payload)))
    assert result.returncode != 0 and not (tmp_path / "artifacts/index-manifest.json").exists()


def test_duplicate_index_members_fail_before_sanitization(tmp_path: Path) -> None:
    payload = json.dumps(index_payload(descriptor("linux", "amd64", DIGEST_A), descriptor("linux", "arm64", DIGEST_B)))
    payload = payload.replace('"schemaVersion": 2', '"schemaVersion": 1, "schemaVersion": 2')
    result = run(script("Inspect and validate the immutable index"), tmp_path, index_env(tmp_path, payload))
    assert result.returncode != 0


def evidence_env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "raw").mkdir(); (tmp_path / "artifacts").mkdir(); (tmp_path / "config").mkdir()
    contract = Path("config/relay_release_safety_contract.json").read_text()
    (tmp_path / "config/relay_release_safety_contract.json").write_text(contract)
    inputs = {"evidence_label": "candidate", "index_digest": DIGEST_A, "release_base": "main", "release_ref": "refs/tags/v1", "source_commit": "c" * 40}
    (tmp_path / "raw/validated-inputs.json").write_text(json.dumps(inputs))
    (tmp_path / "artifacts/index-manifest.json").write_text(json.dumps(index_payload(descriptor("linux", "amd64", DIGEST_A), descriptor("linux", "arm64", DIGEST_B))))
    return {"RAW_DIR": "raw", "ARTIFACT_DIR": "artifacts", "OCI_REPOSITORY": "example.test/relay", "AMD64_DIGEST": DIGEST_A, "ARM64_DIGEST": DIGEST_B, "GATE_COMMIT": "d" * 40}


def write_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arch: str) -> Path:
    platform = f"linux/{arch}"; digest = DIGEST_A if arch == "amd64" else DIGEST_B
    target = tmp_path / f"raw/relay-release-safety-{arch}-evidence.json"
    metrics = {
        "metrics.valid_instrumentation": {"passed": True},
        "metrics.no_flask_defaults": {"passed": True},
        "metrics.no_raw_paths": {"passed": True},
        "metrics.bounded_unmatched_paths": {"passed": True, "batch_size": 1024,
            "total_unmatched_requests": 2048, "first_batch_growth": 1, "second_batch_growth": 0},
    }
    monkeypatch.setattr(gate, "docker_output", lambda *args: (
        "sha256:" + "e" * 64 if args[-1] == "{{.Id}}" else
        "c" * 40 if "revision" in args[-1] else arch
    ))
    monkeypatch.setattr(gate, "validate_registry_identity", lambda *args: None)
    monkeypatch.setattr(gate, "request", lambda *args, **kwargs: (200, ""))
    monkeypatch.setattr(gate, "execute_metrics_checks", lambda *_: metrics)
    monkeypatch.setattr(gate, "execute_public_exemption_check", lambda *_: {"quota.public_information_exempt": {
        "passed": True, "safe_methods_response_class": "2xx", "sentinel_status_codes": [200, 429]}})
    monkeypatch.setattr(gate, "inspect_public_exemption_predicate", lambda *_: True)
    monkeypatch.setattr(gate, "execute_quota_check", lambda _url, result_id, **_kwargs: {
        result_id: {"passed": True, "status_codes": [200 if "mutating" not in result_id else 400, 429]}})
    original_run = subprocess.run
    monkeypatch.setattr(gate.subprocess, "run", lambda args, **kwargs: (
        subprocess.CompletedProcess(args, 0, "", "") if args[:3] == ["docker", "rm", "-f"]
        else original_run(args, **kwargs)
    ))
    monkeypatch.setattr(sys, "argv", ["relay_release_safety_gate.py", "--image", f"example.test/relay@{digest}",
        "--platform", platform, "--source-commit", "c" * 40, "--release-ref", "refs/tags/v1",
        "--release-base", "main", "--registry-coordinate", f"example.test/relay@{digest}",
        "--index-digest", DIGEST_A, "--platform-digest", digest, "--resolved-revision", "c" * 40,
        "--port", "15012", "--evidence", str(target)])
    assert gate.main() == 0
    (tmp_path / f"raw/{arch}-outcome.json").write_text('{"cleanup_status":0,"gate_status":0}')
    return target


def test_current_gate_evidence_succeeds_with_exact_hashes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = evidence_env(tmp_path); write_evidence(monkeypatch, tmp_path, "amd64"); write_evidence(monkeypatch, tmp_path, "arm64")
    result = run(script("Verify evidence and assemble bounded record"), tmp_path, env)
    assert result.returncode == 0, result.stderr
    metadata = json.loads((tmp_path / "artifacts/qualification-metadata.json").read_text())
    assert metadata["qualification_passed"] is True and set(metadata["platform_results"].values()) == {"passed"}
    for line in (tmp_path / "artifacts/SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  "); assert hashlib.sha256((tmp_path / "artifacts" / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("corrupt", ["duplicate", "missing", "failed", "identity", "passed_int", "schema_float"])
def test_invalid_evidence_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, corrupt: str) -> None:
    env = evidence_env(tmp_path); write_evidence(monkeypatch, tmp_path, "amd64"); write_evidence(monkeypatch, tmp_path, "arm64")
    path = tmp_path / "raw/relay-release-safety-amd64-evidence.json"; value = json.loads(path.read_text())
    if corrupt == "duplicate": path.write_text(path.read_text().replace('"passed": true', '"passed": false, "passed": true', 1))
    elif corrupt == "missing": value["results"].pop(next(iter(value["results"]))); path.write_text(json.dumps(value))
    elif corrupt == "failed": value["results"][next(iter(value["results"]))]["passed"] = False; path.write_text(json.dumps(value))
    elif corrupt == "identity": value["platform_digest"] = DIGEST_B; path.write_text(json.dumps(value))
    elif corrupt == "passed_int": value["passed"] = 1; path.write_text(json.dumps(value))
    else: value["schema_version"] = 2.0; path.write_text(json.dumps(value))
    result = run(script("Verify evidence and assemble bounded record"), tmp_path, env)
    assert result.returncode != 0
    assert json.loads((tmp_path / "artifacts/relay-release-safety-amd64-evidence.json").read_text())["error_category"] == "evidence_rejected"


@pytest.mark.parametrize("location", ["response_headers", "error_category"])
def test_nested_sensitive_bytes_never_enter_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, location: str) -> None:
    env = evidence_env(tmp_path); path = write_evidence(monkeypatch, tmp_path, "amd64"); write_evidence(monkeypatch, tmp_path, "arm64")
    value = json.loads(path.read_text())
    result = value["results"][next(iter(value["results"]))]
    if location == "response_headers": result[location] = {"authorization": "DO-NOT-UPLOAD"}
    else: value[location] = {"payload": "DO-NOT-UPLOAD"}
    path.write_text(json.dumps(value))
    result = run(script("Verify evidence and assemble bounded record"), tmp_path, env)
    assert result.returncode != 0
    assert b"DO-NOT-UPLOAD" not in b"".join(path.read_bytes() for path in (tmp_path / "artifacts").iterdir())
    assert len(list((tmp_path / "artifacts").iterdir())) == 5
    for line in (tmp_path / "artifacts/SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  "); assert hashlib.sha256((tmp_path / "artifacts" / name).read_bytes()).hexdigest() == digest


def test_gate_runs_both_platforms_and_records_executor_failures(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir(); (tmp_path / "scripts").mkdir(); bindir = tmp_path / "bin"; bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS/docker\"\ncase \"$1 $2\" in 'ps -aq') exit \"${PS_STATUS:-0}\";; esac\nexit 0\n"); (bindir / "docker").chmod(0o755)
    gate = tmp_path / "scripts/relay_release_safety_gate.py"; gate.write_text("import json,os,sys\nopen(os.environ['CALLS']+'/gate','a').write(' '.join(sys.argv[1:])+'\\n')\np=sys.argv[sys.argv.index('--evidence')+1]; json.dump({'schema_version':2},open(p,'w'))\n")
    calls = tmp_path / "calls"; calls.mkdir()
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "CALLS": str(calls), "RAW_DIR": "raw", "OCI_REPOSITORY": "example.test/relay", "AMD64_DIGEST": DIGEST_A, "ARM64_DIGEST": DIGEST_B, "INPUT_SOURCE_COMMIT": SOURCE_COMMIT, "INPUT_RELEASE_REF": "ref", "INPUT_RELEASE_BASE": "base", "INPUT_INDEX_DIGEST": DIGEST_A}
    result = run(script("Pull and qualify both immutable platform descriptors"), tmp_path, env)
    assert result.returncode == 0
    assert all((tmp_path / f"raw/{arch}-outcome.json").exists() for arch in ("amd64", "arm64"))
    docker_calls = (calls / "docker").read_text().splitlines()
    pulls = [line for line in docker_calls if line.startswith("pull ")]
    assert pulls == [f"pull --platform linux/amd64 example.test/relay@{DIGEST_A}",
        f"pull --platform linux/arm64 example.test/relay@{DIGEST_B}"]
    gate_calls = (calls / "gate").read_text().splitlines()
    assert len(gate_calls) == 2
    for arch, digest, port, call in zip(("amd64", "arm64"), (DIGEST_A, DIGEST_B), ("55100", "55200"), gate_calls):
        args = call.split()
        expected = {"--image": f"example.test/relay@{digest}", "--platform": f"linux/{arch}",
            "--source-commit": SOURCE_COMMIT, "--release-ref": "ref", "--release-base": "base",
            "--resolved-revision": SOURCE_COMMIT, "--registry-coordinate": f"example.test/relay@{digest}",
            "--index-digest": DIGEST_A, "--platform-digest": digest, "--port": port,
            "--evidence": f"raw/relay-release-safety-{arch}-evidence.json"}
        assert {flag: args[args.index(flag) + 1] for flag in expected} == expected
    env["PS_STATUS"] = "1"; result = run(script("Pull and qualify both immutable platform descriptors"), tmp_path, env)
    assert result.returncode != 0 and json.loads((tmp_path / "raw/amd64-outcome.json").read_text())["cleanup_status"] != 0


@pytest.mark.parametrize(("qualification", "amd64", "arm64", "cleanup"), [
    (True, "passed", "passed", "success"),
    (False, "failed", "passed", "success"),
    (False, "passed", "passed", "failure"),
    (False, "failed", "failed", "skipped"),
], ids=("success", "gate-failure", "cleanup-failure", "early-validation-failure"))
def test_summary_executes_and_reports_complete_identity(tmp_path: Path, qualification: bool,
        amd64: str, arm64: str, cleanup: str) -> None:
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    early = cleanup == "skipped"
    source = "invalid" if early else SOURCE_COMMIT
    index_digest = "invalid" if early else DIGEST_A
    amd64_digest, arm64_digest = (("unresolved", "unresolved") if early else (DIGEST_A, DIGEST_B))
    gate_commit = "unresolved" if early else "d" * 40
    metadata = {"source_commit": source, "repository": "example.test/relay",
        "index_digest": index_digest,
        "platform_digests": {"linux/amd64": amd64_digest, "linux/arm64": arm64_digest},
        "gate_commit": gate_commit, "platform_results": {"linux/amd64": amd64, "linux/arm64": arm64},
        "qualification_passed": qualification,
        "executor_outcomes": {"inputs": "failure" if early else "success", "cleanup": cleanup}}
    (artifacts / "qualification-metadata.json").write_text(json.dumps(metadata))
    summary = tmp_path / "summary"
    result = run(script("Write qualification summary"), tmp_path, {"ARTIFACT_DIR": "artifacts",
        "ARTIFACT_NAME": "qualification-123", "GITHUB_STEP_SUMMARY": str(summary)})
    assert result.returncode == 0, result.stderr
    text = summary.read_text()
    for expected in (source, f"example.test/relay@{index_digest}", amd64_digest, arm64_digest,
        gate_commit, f"AMD64 result: {amd64}", f"ARM64 result: {arm64}",
        f'"cleanup":"{cleanup}"', f"Qualification passed: {str(qualification).lower()}",
        "Artifact: qualification-123"):
        assert expected in text


@pytest.mark.parametrize("mode", ["list", "remove", "files"])
def test_final_cleanup_propagates_listing_removal_and_file_failures(tmp_path: Path, mode: str) -> None:
    raw = tmp_path / "raw"; raw.mkdir(); (raw / "owned.tmp").write_text("temporary")
    bindir = tmp_path / "bin"; bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = ps ]; then [ \"$MODE\" = list ] && exit 1; echo owned-container; exit 0; fi\n"
        "[ \"$MODE\" = remove ] && exit 1\nexit 0\n"
    ); docker.chmod(0o755)
    if mode == "files":
        find = bindir / "find"; find.write_text("#!/bin/sh\nexit 1\n"); find.chmod(0o755)
    result = run(script("Final owned-resource cleanup before upload"), tmp_path, {"PATH": f"{bindir}:{os.environ['PATH']}", "RAW_DIR": "raw", "MODE": mode})
    assert result.returncode != 0
