#!/usr/bin/env python3
"""Fail-closed behavioral qualification for a built relay release image."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "config/relay_release_safety_contract.json"
EXPECTED_IDS = {
    "metrics.valid_instrumentation", "metrics.no_flask_defaults", "metrics.no_raw_paths",
    "metrics.bounded_unmatched_paths", "quota.public_information_exempt",
    "quota.protected_rate_limited", "quota.protected_daily_limited",
    "quota.mutating_rate_limited", "quota.mutating_daily_limited",
}
UNMATCHED_BATCH_SIZE = 1024
METRICS_RATE_LIMIT = "5000/minute"
METRICS_DAILY_QUOTA = "5000/day"
PUBLIC_INFORMATION_PATHS = ("/", "/api/v1/meta", "/api/v1/version")
PROTECTED_READ_PATH = "/api/v1/models"
MUTATING_PATH = "/api/v1/relay/requests/cancel"
GIT_SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
NUMBER = re.compile(r"[-+]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|NaN|Inf)")
LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


class GateFailure(RuntimeError):
    """The candidate did not satisfy the mandatory release contract."""


@dataclass(frozen=True)
class Sample:
    name: str
    labels: tuple[tuple[str, str], ...]
    # Values change as counters advance and therefore are not series identity.
    value: str = field(default="", compare=False, hash=False)


def load_contract(path: Path = CONTRACT_PATH) -> list[dict[str, str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        requirements = document["requirements"]
        ids = [item["id"] for item in requirements]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateFailure("contract_invalid") from exc
    if document.get("schema_version") != 1 or not requirements or len(ids) != len(set(ids)):
        raise GateFailure("contract_invalid")
    if set(ids) != EXPECTED_IDS or any(not item.get("description") for item in requirements):
        raise GateFailure("contract_mismatch")
    return requirements


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, _fp, code, msg, headers, _newurl):  # noqa: ARG002
        return None


def request(
    base_url: str, path: str, method: str = "GET", body: bytes | None = None,
) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=5) as response:  # nosec B310 -- fixed loopback origin
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return 0, ""


def parse_metrics(text: str) -> set[Sample]:
    """Parse complete Prometheus text sample identities, rejecting malformed input."""
    samples: set[Sample] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            rf"({METRIC_NAME.pattern})(\{{(?:[^\"\\}}]|\\.|\"(?:\\.|[^\"\\])*\")*\}})?"
            rf"\s+({NUMBER.pattern})(?:\s+\d+)?",
            line,
        )
        if not match:
            raise GateFailure("metrics_malformed")
        name, labels_block, numeric_value = match.groups()
        labels_text = labels_block[1:-1] if labels_block else ""
        labels: list[tuple[str, str]] = []
        pos = 0
        while pos < len(labels_text):
            if labels and labels_text[pos] != ",":
                raise GateFailure("metrics_malformed")
            if labels:
                pos += 1
            item = re.match(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:\\.|[^"\\])*)"\s*', labels_text[pos:])
            if not item or not LABEL_NAME.fullmatch(item.group(1)):
                raise GateFailure("metrics_malformed")
            try:
                label_value = json.loads('"' + item.group(2) + '"')
            except json.JSONDecodeError as exc:
                raise GateFailure("metrics_malformed") from exc
            labels.append((item.group(1), label_value))
            pos += item.end()
        if len({key for key, _ in labels}) != len(labels):
            raise GateFailure("metrics_malformed")
        samples.add(Sample(name, tuple(sorted(labels)), numeric_value))
    if not samples:
        raise GateFailure("metrics_empty")
    return samples


def execute_metrics_checks(base_url: str) -> dict[str, dict[str, object]]:
    results = {key: {"passed": False} for key in EXPECTED_IDS if key.startswith("metrics.")}
    status, warm = request(base_url, "/metrics")
    if status != 200:
        return results
    try:
        parse_metrics(warm)  # warm lazy collectors before the baseline
        status, before_text = request(base_url, "/metrics")
        if status != 200:
            return results
        before = parse_metrics(before_text)
        first = [f"/release-safety-unmatched-{uuid.uuid4().hex}" for _ in range(UNMATCHED_BATCH_SIZE)]
        if any(request(base_url, path)[0] != 404 for path in first):
            return results
        status, middle_text = request(base_url, "/metrics")
        if status != 200:
            return results
        middle = parse_metrics(middle_text)
        second = [f"/release-safety-unmatched-{uuid.uuid4().hex}" for _ in range(UNMATCHED_BATCH_SIZE)]
        if any(request(base_url, path)[0] != 404 for path in second):
            return results
        status, after_text = request(base_url, "/metrics")
        if status != 200:
            return results
        after = parse_metrics(after_text)
    except GateFailure:
        return results
    names = {sample.name for sample in after}
    expected = {"tokenplace_http_requests_total", "tokenplace_instrumentation_up"}
    instrumentation = [sample for sample in after if sample.name == "tokenplace_instrumentation_up"]
    results["metrics.valid_instrumentation"] = {
        "passed": expected <= names and bool(instrumentation)
        and all(float(sample.value) == 1 for sample in instrumentation)
    }
    results["metrics.no_flask_defaults"] = {"passed": not any(n.startswith("flask_http_") for n in names)}
    all_paths = first + second
    unsafe = False
    # Bounded collectors may create their fixed ``unknown``/``other`` series
    # lazily during the first unmatched batch. Only continued identity growth
    # in the successive batch demonstrates transformed per-path labels.
    continued_growth = after - middle
    for sample in after:
        for key, value in sample.labels:
            # Flask endpoint names and normalized route templates are bounded; the
            # presence of an ``endpoint`` label alone is therefore not unsafe.
            raw_path = value.startswith("/") and (
                "release-safety-unmatched-" in value
                or bool(re.search(r"/(?:[0-9a-f]{16,}|\d{6,})(?:/|$)", value, re.IGNORECASE))
            )
            transformed_per_path = sample in continued_growth and key in {"path", "route", "endpoint", "url"}
            if raw_path or transformed_per_path or value in all_paths or "release-safety-unmatched-" in value:
                unsafe = True
    results["metrics.no_raw_paths"] = {"passed": not unsafe}
    growth1, growth2 = len(middle - before), len(after - middle)
    results["metrics.bounded_unmatched_paths"] = {
        "passed": growth2 == 0,
        "batch_size": UNMATCHED_BATCH_SIZE,
        "total_unmatched_requests": 2 * UNMATCHED_BATCH_SIZE,
        "first_batch_growth": growth1,
        "second_batch_growth": growth2,
    }
    return results


def execute_public_exemption_check(base_url: str) -> dict[str, dict[str, object]]:
    public = [request(base_url, path, method)[0] for method in ("GET", "HEAD")
              for path in PUBLIC_INFORMATION_PATHS for _ in range(2)]
    # A near-match remains non-mutating (404) and must consume quota, proving
    # that exemption matching is exact rather than prefix-based. Separate POST
    # quota phases below prove mutating methods are not generally exempt.
    near_match = [request(base_url, "/api/v1/meta/release-safety-near-match")[0] for _ in range(2)]
    return {"quota.public_information_exempt": {
        "passed": all(code == 200 for code in public) and near_match == [404, 429],
        "safe_methods_response_class": "2xx" if all(code == 200 for code in public) else "unexpected",
        "near_match_response_classes": [f"{code // 100}xx" for code in near_match],
    }}


def execute_quota_check(base_url: str, limit_id: str, *, mutating: bool) -> dict[str, dict[str, object]]:
    if mutating:
        statuses = [request(base_url, MUTATING_PATH, "POST", b"{}")[0] for _ in range(2)]
        expected_first = 400
    else:
        statuses = [request(base_url, PROTECTED_READ_PATH)[0] for _ in range(2)]
        expected_first = 200
    return {limit_id: {
        "passed": statuses == [expected_first, 429],
        "response_classes": [f"{code // 100}xx" for code in statuses],
    }}


def validate_candidate_identity(source: str, revision: str, resolved_revision: str | None = None) -> None:
    source = source.lower()
    revision = revision.lower()
    if not GIT_SHA.fullmatch(source):
        raise GateFailure("source_identity_invalid")
    if GIT_SHA.fullmatch(revision):
        if revision != source:
            raise GateFailure("source_identity_mismatch")
    elif not (re.fullmatch(r"[0-9a-f]{7,39}", revision) and resolved_revision == source and source.startswith(revision)):
        raise GateFailure("source_identity_mismatch")


def validate_registry_identity(
    image: str, platform: str, coordinate: str | None, index_digest: str | None,
    platform_digest: str | None,
) -> None:
    """Bind publication metadata to the locally resolved platform manifest."""
    supplied = (coordinate, index_digest, platform_digest)
    if not any(supplied):
        return
    if not all(supplied) or not DIGEST.fullmatch(index_digest or "") or not DIGEST.fullmatch(platform_digest or ""):
        raise GateFailure("registry_identity_invalid")
    if not coordinate or coordinate.count("@") != 1:
        raise GateFailure("registry_identity_invalid")
    repository, coordinate_digest = coordinate.rsplit("@", 1)
    if not repository or re.search(r"\s", repository) or coordinate_digest != platform_digest:
        raise GateFailure("registry_identity_mismatch")

    try:
        repo_digests = json.loads(docker_output("image", "inspect", image, "--format", "{{json .RepoDigests}}"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise GateFailure("registry_identity_invalid") from exc
    if not isinstance(repo_digests, list) or coordinate not in repo_digests:
        raise GateFailure("local_image_identity_mismatch")
    try:
        index = json.loads(docker_output("buildx", "imagetools", "inspect", "--raw", f"{repository}@{index_digest}"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise GateFailure("registry_identity_invalid") from exc
    os_name, architecture = platform.split("/", 1)
    members = [
        item for item in index.get("manifests", [])
        if isinstance(item, dict) and item.get("digest") == platform_digest
        and item.get("platform", {}).get("os") == os_name
        and item.get("platform", {}).get("architecture") == architecture
    ] if isinstance(index, dict) else []
    if len(members) != 1:
        raise GateFailure("index_platform_mismatch")


def docker_output(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=30).stdout.strip()


def _category(exc: BaseException) -> str:
    if isinstance(exc, GateFailure):
        return str(exc) if re.fullmatch(r"[a-z_]+", str(exc)) else "qualification_failed"
    if isinstance(exc, FileNotFoundError):
        return "runtime_missing"
    if isinstance(exc, subprocess.TimeoutExpired):
        return "runtime_timeout"
    if isinstance(exc, subprocess.CalledProcessError):
        return "runtime_command_failed"
    return "runtime_unavailable"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", required=True, choices=("linux/amd64", "linux/arm64"))
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-ref", required=True)
    parser.add_argument("--release-base", required=True)
    parser.add_argument("--registry-coordinate")
    parser.add_argument("--index-digest")
    parser.add_argument("--platform-digest")
    parser.add_argument("--resolved-revision")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--port", type=int, default=15012)
    args = parser.parse_args()
    requirements: list[dict[str, str]] = []
    evidence: dict[str, object] = {
        "schema_version": 2, "source_commit": args.source_commit, "release_ref": args.release_ref,
        "release_base": args.release_base, "registry_coordinate": args.registry_coordinate or args.image,
        "index_digest": args.index_digest, "platform": args.platform, "platform_digest": args.platform_digest,
        "contract": str(CONTRACT_PATH.relative_to(ROOT)), "results": {}, "passed": False,
    }
    containers: list[str] = []
    try:
        requirements = load_contract()
        evidence["results"] = {item["id"]: {"state": "not_run", "passed": False} for item in requirements}
        # Resolve a mutable alias once, before metadata checks, so provenance and probes
        # cannot be split across different images if that alias changes mid-gate.
        image_id = docker_output("image", "inspect", args.image, "--format", "{{.Id}}")
        if not DIGEST.fullmatch(image_id):
            raise GateFailure("local_image_identity_invalid")
        evidence["image_id"] = image_id
        revision = docker_output("image", "inspect", image_id, "--format", '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
        architecture = docker_output("image", "inspect", image_id, "--format", "{{.Architecture}}")
        if architecture != args.platform.split("/")[1]:
            raise GateFailure("platform_identity_mismatch")
        validate_candidate_identity(args.source_commit, revision, args.resolved_revision)
        validate_registry_identity(image_id, args.platform, args.registry_coordinate, args.index_digest,
                                   args.platform_digest)

        phases = [
            ("metrics", METRICS_RATE_LIMIT, METRICS_DAILY_QUOTA, None),
            ("public", "1/minute", "1/day", "public"),
            ("protected-rate", "1/minute", "5000/day", "quota.protected_rate_limited"),
            ("protected-daily", "5000/minute", "1/day", "quota.protected_daily_limited"),
            ("mutating-rate", "1/minute", "5000/day", "quota.mutating_rate_limited"),
            ("mutating-daily", "5000/minute", "1/day", "quota.mutating_daily_limited"),
        ]
        for offset, (phase, rate, daily, check_id) in enumerate(phases):
            container = f"relay-safety-{phase}-{uuid.uuid4().hex[:8]}"
            containers.append(container)
            docker_output("run", "-d", "--rm", "--name", container, "-p", f"127.0.0.1:{args.port + offset}:5010",
                          "-e", "TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0", "-e", f"API_RATE_LIMIT={rate}",
                          "-e", f"API_DAILY_QUOTA={daily}", image_id)
            base = f"http://127.0.0.1:{args.port + offset}"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if request(base, "/livez")[0] == 200:
                    break
                time.sleep(.5)
            else:
                raise GateFailure("startup_timeout")
            if phase == "metrics":
                checked = execute_metrics_checks(base)
            elif phase == "public":
                checked = execute_public_exemption_check(base)
            else:
                assert check_id is not None
                checked = execute_quota_check(base, check_id, mutating=phase.startswith("mutating-"))
            for key, result in checked.items():
                result["state"] = "passed" if result["passed"] else "failed"
                evidence["results"][key] = result
        evidence["passed"] = all(v.get("state") == "passed" for v in evidence["results"].values())
        if not evidence["passed"]:
            evidence["error_category"] = "mandatory_check_failed"
    except (GateFailure, subprocess.SubprocessError, OSError) as exc:
        evidence["error_category"] = _category(exc)
    finally:
        cleanup_failed = False
        for container in containers:
            try:
                completed = subprocess.run(
                    ["docker", "rm", "-f", container], check=False, capture_output=True, timeout=15
                )
                if completed.returncode != 0:
                    cleanup_failed = True
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
        if cleanup_failed:
            evidence["cleanup"] = "failed"
            evidence["passed"] = False
            evidence.setdefault("error_category", "cleanup_failed")
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["passed"] is True else 1


if __name__ == "__main__":
    sys.exit(main())
