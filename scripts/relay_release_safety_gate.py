#!/usr/bin/env python3
"""Exercise the mandatory relay release-safety contract over HTTP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_CONTRACT = Path("config/relay_release_safety_contract.json")
IMPLEMENTED_CHECKS = {
    "metrics.no_flask_defaults",
    "metrics.no_raw_paths",
    "metrics.bounded_unmatched_paths",
    "quota.public_information_exempt",
    "quota.protected_routes_limited",
}
FLASK_DEFAULT_PREFIXES = ("flask_http_request_", "flask_exporter_info")


def _get(base_url: str, path: str) -> tuple[int, str]:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("safety gate base URL must be an HTTP loopback address")
    if parsed.username or parsed.password:
        raise ValueError("safety gate base URL must not contain credentials")
    try:
        # The scheme and host are constrained above; paths are gate-owned constants/nonces.
        with urllib.request.urlopen(f"{base_url}{path}", timeout=10) as response:  # nosec B310
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _series(exposition: str) -> set[str]:
    return {
        line.rsplit(None, 1)[0]
        for line in exposition.splitlines()
        if line and not line.startswith("#")
    }


def load_contract(path: Path) -> dict:
    contract = json.loads(path.read_text(encoding="utf-8"))
    if contract.get("schema_version") != 1:
        raise ValueError("unsupported or missing contract schema_version")
    requirements = contract.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("requirements must be a non-empty list")
    ids = [item.get("id") if isinstance(item, dict) else None for item in requirements]
    if any(not isinstance(item, str) or not item for item in ids):
        raise ValueError("every requirement must have a non-empty id")
    if len(ids) != len(set(ids)):
        raise ValueError("requirement ids must be unique")
    unknown = set(ids) - IMPLEMENTED_CHECKS
    missing = IMPLEMENTED_CHECKS - set(ids)
    if unknown or missing:
        raise ValueError(f"contract/runner check mismatch (unknown={sorted(unknown)}, missing={sorted(missing)})")
    if not isinstance(contract.get("unmatched_path_count"), int) or contract["unmatched_path_count"] < 2:
        raise ValueError("unmatched_path_count must be an integer of at least 2")
    if not isinstance(contract.get("maximum_series_growth"), int) or contract["maximum_series_growth"] < 0:
        raise ValueError("maximum_series_growth must be a non-negative integer")
    return contract


def run_contract(base_url: str, contract: dict) -> dict[str, bool]:
    before_status, before_text = _get(base_url, "/metrics")
    if before_status != 200:
        raise RuntimeError("metrics endpoint was unavailable")

    nonce = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
    paths = [f"/__release_safety_probe_{nonce}_{index}" for index in range(contract["unmatched_path_count"])]
    if any(_get(base_url, path)[0] != 404 for path in paths):
        raise RuntimeError("unmatched-path probe did not consistently return 404")
    after_status, after_text = _get(base_url, "/metrics")
    if after_status != 200:
        raise RuntimeError("metrics endpoint became unavailable")

    metric_names = {
        re.split(r"[{ ]", line, maxsplit=1)[0]
        for line in after_text.splitlines()
        if line and not line.startswith("#")
    }
    results = {
        "metrics.no_flask_defaults": not any(
            name.startswith(FLASK_DEFAULT_PREFIXES) for name in metric_names
        ),
        "metrics.no_raw_paths": not any(path in after_text for path in paths),
        "metrics.bounded_unmatched_paths": len(_series(after_text) - _series(before_text))
        <= contract["maximum_series_growth"],
    }

    public_statuses = [
        _get(base_url, path)[0]
        for path in ("/", "/api/v1/meta", "/api/v1/version")
        for _ in range(3)
    ]
    protected_statuses = [_get(base_url, "/api/v1/models")[0] for _ in range(3)]
    results["quota.public_information_exempt"] = all(status == 200 for status in public_statuses)
    results["quota.protected_routes_limited"] = protected_statuses[-1] == 429 and any(
        status != 429 for status in protected_statuses[:-1]
    )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-ref", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--candidate-digest", required=True)
    args = parser.parse_args()

    try:
        contract = load_contract(args.contract)
        results = run_contract(args.base_url.rstrip("/"), contract)
        evidence = {
            "schema_version": 1,
            "source_commit": args.source_commit,
            "release_ref": args.release_ref,
            "candidate_image": args.candidate_image,
            "candidate_digest": args.candidate_digest,
            "contract": str(args.contract),
            "results": [{"id": item["id"], "passed": results[item["id"]]} for item in contract["requirements"]],
        }
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        for result in evidence["results"]:
            print(f'{"PASS" if result["passed"] else "FAIL"}: {result["id"]}')
        return 0 if all(results.values()) else 1
    except Exception as exc:
        print(f"release safety gate failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
