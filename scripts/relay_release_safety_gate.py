#!/usr/bin/env python3
"""Fail-closed behavioral qualification for a built relay container image."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "config" / "relay_release_safety_contract.json"
REQUIRED_FIELDS = {
    "schema_version",
    "contract_id",
    "required_checks",
    "unmatched_path_probe_count",
    "maximum_unmatched_path_series_growth",
    "default_metric_prefixes",
    "public_information_paths",
    "protected_path",
}
IMPLEMENTED_CHECKS = {
    "metrics.default_families_absent",
    "metrics.request_path_labels_bounded",
    "metrics.unmatched_path_series_bounded",
    "quota.public_information_reads_exempt",
    "quota.protected_route_enforced",
}


def load_contract(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != REQUIRED_FIELDS:
        raise ValueError("safety contract fields are missing or malformed")
    checks = data["required_checks"]
    if not isinstance(checks, list) or set(checks) != IMPLEMENTED_CHECKS or len(checks) != len(set(checks)):
        raise ValueError("required_checks must contain every implemented check exactly once")
    if data["schema_version"] != 1 or not isinstance(data["contract_id"], str):
        raise ValueError("unsupported safety contract schema")
    if not isinstance(data["unmatched_path_probe_count"], int) or data["unmatched_path_probe_count"] < 10:
        raise ValueError("unmatched_path_probe_count must be an integer of at least 10")
    if not isinstance(data["maximum_unmatched_path_series_growth"], int) or data["maximum_unmatched_path_series_growth"] < 0:
        raise ValueError("maximum_unmatched_path_series_growth must be a non-negative integer")
    if data["public_information_paths"] != ["/", "/api/v1/meta", "/api/v1/version"]:
        raise ValueError("all incident-critical public information paths are required")
    return data


def request(base_url: str, path: str, method: str = "GET") -> tuple[int, str]:
    req = urllib.request.Request(base_url + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def metric_series(body: str) -> set[str]:
    return {
        line.split(None, 1)[0]
        for line in body.splitlines()
        if line and not line.startswith("#") and " " in line
    }


def evaluate(base_url: str, contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    status, before_body = request(base_url, "/metrics")
    if status != 200:
        raise RuntimeError("metrics endpoint was unavailable")
    before = metric_series(before_body)
    prefixes = tuple(contract["default_metric_prefixes"])
    defaults_absent = not any(name.startswith(prefixes) for name in before)
    results["metrics.default_families_absent"] = {"passed": defaults_absent}

    nonce = secrets.token_hex(8)
    for index in range(contract["unmatched_path_probe_count"]):
        request(base_url, f"/__release_safety_probe_{nonce}_{index}")
    status, after_body = request(base_url, "/metrics")
    if status != 200:
        raise RuntimeError("metrics endpoint became unavailable")
    after = metric_series(after_body)
    raw_absent = nonce not in after_body
    growth = len(after - before)
    results["metrics.request_path_labels_bounded"] = {"passed": raw_absent}
    results["metrics.unmatched_path_series_bounded"] = {
        "passed": growth <= contract["maximum_unmatched_path_series_growth"],
        "observed_series_growth": growth,
        "maximum_series_growth": contract["maximum_unmatched_path_series_growth"],
    }

    public_ok = True
    for path in contract["public_information_paths"]:
        for method in ("GET", "HEAD"):
            public_ok = public_ok and all(request(base_url, path, method)[0] != 429 for _ in range(3))
    results["quota.public_information_reads_exempt"] = {"passed": public_ok}

    protected_statuses = [request(base_url, contract["protected_path"])[0] for _ in range(2)]
    results["quota.protected_route_enforced"] = {
        "passed": protected_statuses[0] != 429 and protected_statuses[1] == 429,
        "observed_statuses": protected_statuses,
    }
    return results


def inspect_image(runtime: str, image: str) -> str:
    result = subprocess.run(
        [runtime, "image", "inspect", "--format", "{{.Id}}", image],
        check=True,
        capture_output=True,
        text=True,
    )
    image_id = result.stdout.strip()
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise RuntimeError("container runtime returned a malformed immutable image ID")
    return image_id


def require_expected_image_id(actual: str, expected: str | None) -> None:
    if expected and actual != expected:
        raise RuntimeError("candidate image immutable identity does not match expected identity")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--runtime", default=os.environ.get("CONTAINER_RUNTIME", "docker"))
    parser.add_argument("--port", type=int, default=5012)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-ref", required=True)
    parser.add_argument("--expected-image-id")
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    contract = load_contract(args.contract)
    image_id = inspect_image(args.runtime, args.image)
    require_expected_image_id(image_id, args.expected_image_id)

    name = f"tokenplace-release-safety-{os.getpid()}"
    command = [
        args.runtime, "run", "-d", "--rm", "--name", name,
        "-p", f"127.0.0.1:{args.port}:5010",
        "-e", "TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0",
        "-e", "API_RATE_LIMIT=1/minute", "-e", "API_DAILY_QUOTA=1/day", args.image,
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    try:
        base_url = f"http://127.0.0.1:{args.port}"
        for _ in range(30):
            try:
                if request(base_url, "/livez")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError("candidate container did not become ready")
        results = evaluate(base_url, contract)
    finally:
        subprocess.run([args.runtime, "stop", name], check=False, stdout=subprocess.DEVNULL)

    evidence = {
        "schema_version": 1,
        "contract_id": contract["contract_id"],
        "contract_sha256": hashlib.sha256(args.contract.read_bytes()).hexdigest(),
        "source_commit": args.source_commit,
        "release_ref": args.release_ref,
        "candidate_image": args.image,
        "candidate_image_id": image_id,
        "results": results,
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = [name for name in contract["required_checks"] if not results.get(name, {}).get("passed")]
    if failed:
        print("Release safety gate failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"Release safety gate passed ({contract['contract_id']}, {image_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
