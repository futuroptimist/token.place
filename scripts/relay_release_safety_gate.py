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
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "config/relay_release_safety_contract.json"
EXPECTED_IDS = {
    "metrics.valid_instrumentation", "metrics.no_flask_defaults", "metrics.no_raw_paths",
    "metrics.bounded_unmatched_paths", "quota.public_information_exempt",
    "quota.protected_rate_limited", "quota.protected_daily_limited",
}
GIT_SHA = re.compile(r"[0-9a-f]{40}")
METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
NUMBER = re.compile(r"[-+]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|NaN|Inf)")
LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


class GateFailure(RuntimeError):
    """The candidate did not satisfy the mandatory release contract."""


@dataclass(frozen=True)
class Sample:
    name: str
    labels: tuple[tuple[str, str], ...]


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


def request(base_url: str, path: str, method: str = "GET") -> tuple[int, str]:
    req = urllib.request.Request(f"{base_url}{path}", method=method)
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
        match = re.fullmatch(r"([^\s]+)\s+([^\s]+)(?:\s+(\d+))?", line)
        if not match or not NUMBER.fullmatch(match.group(2)):
            raise GateFailure("metrics_malformed")
        identity = match.group(1)
        if "{" in identity:
            name, labels_text = identity.split("{", 1)
            if not labels_text.endswith("}"):
                raise GateFailure("metrics_malformed")
            labels_text = labels_text[:-1]
        else:
            name, labels_text = identity, ""
        if not METRIC_NAME.fullmatch(name):
            raise GateFailure("metrics_malformed")
        labels: list[tuple[str, str]] = []
        pos = 0
        while pos < len(labels_text):
            item = re.match(r'(?:,)?([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"', labels_text[pos:])
            if not item or not LABEL_NAME.fullmatch(item.group(1)):
                raise GateFailure("metrics_malformed")
            try:
                value = json.loads('"' + item.group(2) + '"')
            except json.JSONDecodeError as exc:
                raise GateFailure("metrics_malformed") from exc
            labels.append((item.group(1), value))
            pos += item.end()
        samples.add(Sample(name, tuple(sorted(labels))))
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
        before = parse_metrics(before_text) if status == 200 else set()
        first = [f"/release-safety-unmatched-{uuid.uuid4().hex}" for _ in range(24)]
        if any(request(base_url, path)[0] != 404 for path in first):
            return results
        status, middle_text = request(base_url, "/metrics")
        middle = parse_metrics(middle_text) if status == 200 else set()
        second = [f"/release-safety-unmatched-{uuid.uuid4().hex}" for _ in range(24)]
        if any(request(base_url, path)[0] != 404 for path in second):
            return results
        status, after_text = request(base_url, "/metrics")
        after = parse_metrics(after_text) if status == 200 else set()
    except GateFailure:
        return results
    names = {sample.name for sample in after}
    expected = {"tokenplace_http_requests_total", "tokenplace_instrumentation_up"}
    results["metrics.valid_instrumentation"] = {"passed": status == 200 and expected <= names}
    results["metrics.no_flask_defaults"] = {"passed": not any(n.startswith("flask_http_") for n in names)}
    all_paths = first + second
    unsafe = False
    for sample in after:
        for key, value in sample.labels:
            if key in {"path", "endpoint", "url"} or value in all_paths or "release-safety-unmatched-" in value:
                unsafe = True
    results["metrics.no_raw_paths"] = {"passed": not unsafe}
    growth1, growth2 = len(middle - before), len(after - middle)
    results["metrics.bounded_unmatched_paths"] = {
        "passed": growth2 == 0, "first_batch_growth": growth1, "second_batch_growth": growth2,
    }
    return results


def execute_quota_checks(base_url: str, limit_id: str) -> dict[str, dict[str, object]]:
    public = [request(base_url, path, method)[0] for method in ("GET", "HEAD")
              for path in ("/", "/api/v1/meta", "/api/v1/version") for _ in range(2)]
    first = request(base_url, "/api/v1/models")[0]
    second = request(base_url, "/api/v1/models")[0]
    return {
        "quota.public_information_exempt": {"passed": all(code == 200 for code in public)},
        limit_id: {"passed": first == 200 and second == 429},
    }


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
        revision = docker_output("image", "inspect", args.image, "--format", '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
        architecture = docker_output("image", "inspect", args.image, "--format", "{{.Architecture}}")
        if architecture != args.platform.split("/")[1]:
            raise GateFailure("platform_identity_mismatch")
        validate_candidate_identity(args.source_commit, revision, args.resolved_revision)
        evidence["image_id"] = docker_output("image", "inspect", args.image, "--format", "{{.Id}}")

        phases = [("metrics", "1000/minute", "1000/day"), ("rate", "1/minute", "1000/day"), ("daily", "1000/minute", "1/day")]
        for offset, (phase, rate, daily) in enumerate(phases):
            container = f"relay-safety-{phase}-{uuid.uuid4().hex[:8]}"
            containers.append(container)
            docker_output("run", "-d", "--rm", "--name", container, "-p", f"127.0.0.1:{args.port + offset}:5010",
                          "-e", "TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0", "-e", f"API_RATE_LIMIT={rate}",
                          "-e", f"API_DAILY_QUOTA={daily}", args.image)
            base = f"http://127.0.0.1:{args.port + offset}"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if request(base, "/livez")[0] == 200:
                    break
                time.sleep(.5)
            else:
                raise GateFailure("startup_timeout")
            checked = execute_metrics_checks(base) if phase == "metrics" else execute_quota_checks(
                base, f"quota.protected_{phase}_limited")
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
                subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
        if cleanup_failed:
            evidence["cleanup"] = "failed"
            evidence["passed"] = False
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["passed"] is True else 1


if __name__ == "__main__":
    sys.exit(main())
