#!/usr/bin/env python3
"""Qualify a built relay image against the mandatory release-safety contract."""

from __future__ import annotations

import argparse
import json
import re
import secrets
# Commands use fixed argv and a container runtime restricted by argparse.
import subprocess  # nosec B404
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "config/relay_release_safety_contract.json"
REQUIRED_CHECKS = frozenset(
    {
        "bounded_metrics.no_flask_defaults",
        "bounded_metrics.no_raw_paths",
        "bounded_metrics.series_growth",
        "quota.public_information_exempt",
        "quota.protected_route_limited",
    }
)
METRIC_NAME_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)")


class GateError(RuntimeError):
    """The candidate did not provide complete, successful safety evidence."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str


def load_contract(path: Path) -> list[dict[str, str]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        requirements = document["requirements"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise GateError(f"invalid safety contract: {exc}") from exc
    if document.get("schema_version") != 1 or not isinstance(requirements, list):
        raise GateError("invalid safety contract schema")
    ids = []
    for item in requirements:
        if not isinstance(item, dict) or set(item) != {"id", "description"}:
            raise GateError("malformed safety requirement")
        if not all(isinstance(item[key], str) and item[key].strip() for key in item):
            raise GateError("empty safety requirement field")
        ids.append(item["id"])
    if len(ids) != len(set(ids)) or set(ids) != REQUIRED_CHECKS:
        raise GateError(
            "contract must contain every implemented mandatory check exactly once"
        )
    return requirements


def prometheus_series(body: str) -> set[str]:
    series = set()
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        match = METRIC_NAME_RE.match(line)
        if not match or " " not in line:
            raise GateError("malformed Prometheus exposition")
        series.add(line.split(" ", 1)[0])
    if not series:
        raise GateError("metrics exposition contained no series")
    return series


def evaluate_candidate(
    fetch: Callable[[str], HttpResponse],
) -> dict[str, dict[str, object]]:
    """Exercise behavior through HTTP; return only aggregate, privacy-safe evidence."""
    baseline = fetch("/metrics")
    if baseline.status != 200:
        raise GateError("metrics endpoint was not successfully scraped")
    baseline_series = prometheus_series(baseline.body)
    public_statuses = {
        path: [fetch(path).status for _ in range(3)]
        for path in ("/", "/api/v1/meta", "/api/v1/version")
    }
    public_exempt = all(
        status == 200 for values in public_statuses.values() for status in values
    )
    protected_statuses = [fetch("/api/v1/models").status for _ in range(2)]
    protected_limited = protected_statuses[0] == 200 and protected_statuses[1] == 429

    canary_prefix = f"/__release_safety_{secrets.token_hex(8)}_"
    unmatched_count = 64
    for index in range(unmatched_count):
        response = fetch(f"{canary_prefix}{index}")
        if response.status not in {404, 429}:
            raise GateError("unmatched-path probe returned an unexpected status")
    after = fetch("/metrics")
    if after.status != 200:
        raise GateError("post-probe metrics scrape failed")
    after_series = prometheus_series(after.body)
    no_defaults = not any(
        name.startswith("flask_http_request")
        or name.startswith("prometheus_flask_exporter")
        for name in after_series
    )
    no_raw_paths = canary_prefix not in after.body
    growth = len(after_series) - len(baseline_series)
    bounded_growth = growth < unmatched_count // 4

    return {
        "bounded_metrics.no_flask_defaults": {"passed": no_defaults},
        "bounded_metrics.no_raw_paths": {"passed": no_raw_paths},
        "bounded_metrics.series_growth": {
            "passed": bounded_growth,
            "baseline_series": len(baseline_series),
            "final_series": len(after_series),
            "unmatched_requests": unmatched_count,
        },
        "quota.public_information_exempt": {"passed": public_exempt},
        "quota.protected_route_limited": {"passed": protected_limited},
    }


def _run(runtime: str, *args: str, capture: bool = False) -> str:
    result = subprocess.run(  # nosec B603
        [runtime, *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def _fetcher(base_url: str) -> Callable[[str], HttpResponse]:
    def fetch(path: str) -> HttpResponse:
        try:
            # base_url is constructed internally from the loopback container port.
            with urllib.request.urlopen(
                base_url + path, timeout=10
            ) as response:  # nosec B310
                return HttpResponse(
                    response.status, response.read().decode("utf-8", "replace")
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, exc.read().decode("utf-8", "replace"))

    return fetch


def qualify(args: argparse.Namespace) -> dict[str, object]:
    requirements = load_contract(args.contract)
    image_id = _run(
        args.runtime,
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        args.image,
        capture=True,
    )
    revision = _run(
        args.runtime,
        "image",
        "inspect",
        "--format",
        '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
        args.image,
        capture=True,
    )
    if revision != args.source_commit:
        raise GateError(
            "candidate OCI source revision does not match the requested source commit"
        )

    name = f"tokenplace-release-safety-{secrets.token_hex(6)}"
    try:
        _run(
            args.runtime,
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "127.0.0.1::5010",
            "-e",
            "TOKENPLACE_RELAY_REQUIRE_UPSTREAM_HEALTH=0",
            "-e",
            "API_RATE_LIMIT=1/minute",
            "-e",
            "API_DAILY_QUOTA=1/day",
            args.image,
        )
        port = _run(args.runtime, "port", name, "5010/tcp", capture=True).rsplit(
            ":", 1
        )[-1]
        fetch = _fetcher(f"http://127.0.0.1:{port}")
        for _ in range(60):
            try:
                if fetch("/livez").status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise GateError("candidate container did not become ready")
        results = evaluate_candidate(fetch)
    finally:
        subprocess.run(  # nosec B603
            [args.runtime, "rm", "-f", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    if set(results) != {item["id"] for item in requirements}:
        raise GateError("safety evidence is incomplete")
    failures = [
        check_id
        for check_id, result in results.items()
        if result.get("passed") is not True
    ]
    if failures:
        raise GateError("mandatory safety checks failed: " + ", ".join(failures))
    return {
        "schema_version": 1,
        "source_commit": args.source_commit,
        "release_ref": args.release_ref,
        "release_base": args.release_base,
        "candidate_image": args.image,
        "candidate_digest": image_id,
        "qualification_basis": "artifact_behavior",
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-ref", required=True)
    parser.add_argument("--release-base", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--runtime", choices=("docker", "podman"), default="docker")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        evidence = qualify(args)
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (GateError, subprocess.CalledProcessError, OSError) as exc:
        print(f"release safety gate failed: {exc}", file=__import__("sys").stderr)
        return 1
    print(f"release safety gate passed; evidence: {args.evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
