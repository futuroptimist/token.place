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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "config/relay_release_safety_contract.json"
EXPECTED_IDS = {
    "metrics.no_flask_defaults",
    "metrics.no_raw_paths",
    "metrics.bounded_unmatched_paths",
    "quota.public_information_exempt",
    "quota.protected_route_limited",
}
SERIES_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*(?:\{.*\})?\s+[-+0-9.NaInf]+(?:\s+\d+)?$")


class GateFailure(RuntimeError):
    """The candidate did not satisfy the mandatory release contract."""


def load_contract(path: Path = CONTRACT_PATH) -> list[dict[str, str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        requirements = document["requirements"]
        ids = [item["id"] for item in requirements]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateFailure(f"safety contract is missing or malformed: {exc}") from exc
    if document.get("schema_version") != 1 or not requirements or len(ids) != len(set(ids)):
        raise GateFailure("safety contract has an unsupported schema, is empty, or has duplicate ids")
    if set(ids) != EXPECTED_IDS or any(not item.get("description") for item in requirements):
        raise GateFailure("safety contract requirements and executable checks are not identical")
    return requirements


def request(base_url: str, path: str, method: str = "GET") -> tuple[int, str]:
    req = urllib.request.Request(f"{base_url}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError:
        return 0, ""


def series_count(metrics: str) -> int:
    return sum(bool(SERIES_RE.match(line)) for line in metrics.splitlines() if not line.startswith("#"))


def execute_checks(base_url: str) -> dict[str, dict[str, object]]:
    results: dict[str, dict[str, object]] = {}
    _, before = request(base_url, "/metrics")
    probes = [f"/release-safety-unmatched-{uuid.uuid4().hex}" for _ in range(40)]
    for path in probes:
        request(base_url, path)
    status, after = request(base_url, "/metrics")

    results["metrics.no_flask_defaults"] = {
        "passed": status == 200 and "flask_http_request" not in after and "prometheus_flask_exporter" not in after
    }
    results["metrics.no_raw_paths"] = {
        "passed": all(path not in after for path in probes)
        and not re.search(r'\bpath="[^"]+"', after)
    }
    growth = series_count(after) - series_count(before)
    results["metrics.bounded_unmatched_paths"] = {"passed": growth < len(probes), "series_growth": growth}

    public_statuses = []
    for method in ("GET", "HEAD"):
        for path in ("/", "/api/v1/meta", "/api/v1/version"):
            public_statuses.extend(request(base_url, path, method)[0] for _ in range(3))
    first_protected = request(base_url, "/api/v1/models")[0]
    second_protected = request(base_url, "/api/v1/models")[0]
    results["quota.public_information_exempt"] = {
        "passed": 429 not in public_statuses and first_protected != 429
    }
    results["quota.protected_route_limited"] = {"passed": second_protected == 429}
    return results


def qualify(base_url: str, *, contract_path: Path = CONTRACT_PATH) -> dict[str, dict[str, object]]:
    requirements = load_contract(contract_path)
    results = execute_checks(base_url)
    required_ids = {item["id"] for item in requirements}
    if set(results) != required_ids or any(result.get("passed") is not True for result in results.values()):
        failures = sorted(key for key in required_ids if results.get(key, {}).get("passed") is not True)
        raise GateFailure(f"candidate failed mandatory safety checks: {', '.join(failures)}")
    return results


def docker_output(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout.strip()


def validate_candidate_identity(source: str, revision: str, digest: str | None, repo_digests: str = "") -> None:
    if not revision or not (source.startswith(revision) or revision.startswith(source)):
        raise GateFailure("candidate image revision label does not match the requested source commit")
    if digest:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise GateFailure("candidate registry digest is malformed")
        if digest not in repo_digests:
            raise GateFailure("pulled candidate identity does not match the expected registry digest")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Built local image coordinate to qualify")
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-ref", required=True)
    parser.add_argument("--release-base", required=True)
    parser.add_argument("--candidate-digest", help="Expected immutable registry digest")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--port", type=int, default=15012)
    args = parser.parse_args()
    container = f"relay-safety-gate-{uuid.uuid4().hex[:10]}"
    evidence: dict[str, object] = {
        "schema_version": 1,
        "source_commit": args.source_commit,
        "release_ref": args.release_ref,
        "release_base": args.release_base,
        "candidate_image": args.image,
    }
    try:
        revision = docker_output("image", "inspect", args.image, "--format", "{{ index .Config.Labels \"org.opencontainers.image.revision\" }}")
        image_id = docker_output("image", "inspect", args.image, "--format", "{{.Id}}")
        repo_digests = ""
        if args.candidate_digest:
            repo_digests = docker_output("image", "inspect", args.image, "--format", "{{json .RepoDigests}}")
        validate_candidate_identity(args.source_commit, revision, args.candidate_digest, repo_digests)
        evidence["candidate_digest"] = args.candidate_digest or image_id
        docker_output(
            "run", "-d", "--rm", "--name", container, "-p", f"127.0.0.1:{args.port}:5010",
            "-e", "TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0", "-e", "API_RATE_LIMIT=1/minute",
            "-e", "API_DAILY_QUOTA=1/day", args.image,
        )
        base_url = f"http://127.0.0.1:{args.port}"
        for _ in range(30):
            if request(base_url, "/livez")[0] == 200:
                break
            time.sleep(1)
        else:
            raise GateFailure("candidate relay did not become ready")
        evidence["results"] = qualify(base_url)
        evidence["passed"] = True
    except (GateFailure, subprocess.CalledProcessError) as exc:
        evidence["passed"] = False
        evidence["error"] = str(exc)
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence.get("passed") is True else 1


if __name__ == "__main__":
    sys.exit(main())
