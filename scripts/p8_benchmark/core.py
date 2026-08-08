"""P8 packaged-runtime benchmark harness primitives.

The module is intentionally adapter-driven: ordinary tests use canned adapters,
while CLI packaged mode must connect to the real desktop runtime and fail closed
when prerequisites are missing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = "p8-benchmark-report.v1"
FIXTURE_VERSION = "p8-semantic-fixture.v1"
SUMMARY_VERSION = "p8-summary.v1"
TIERS = {"small-8k": 8192, "intermediate-32k": 32768, "64k-full": 55254}
TARGET_DEPTHS = {"early": 0.12, "middle": 0.50, "late": 0.86}
SECRET_PATTERNS = [
    re.compile(r"(?:/[A-Za-z0-9._ -]+){2,}"),
    re.compile(
        r"(?=[A-Za-z0-9+/]{40,}={0,2})(?=[A-Za-z0-9+/]*[+/=])[A-Za-z0-9+/]{40,}={0,2}"
    ),
]
PHASES = ["preparing", "prefill", "generation", "completed", "cancelled", "failed"]


class HarnessError(RuntimeError):
    pass


class SemanticFailure(HarnessError):
    pass


@dataclass(frozen=True)
class TokenCount:
    requested: int
    actual: int
    source: str


class WhitespaceTokenizer:
    source = "whitespace-test-tokenizer"

    def count(self, text: str) -> int:
        return len(text.split())


class RuntimeTokenizer:
    source = "packaged-runtime-tokenizer"

    def __init__(self, command: list[str]):
        self.command = command

    def count(self, text: str) -> int:
        proc = subprocess.run(
            self.command,
            input=text,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )  # noqa: S603
        payload = json.loads(proc.stdout)
        if not isinstance(payload.get("tokens"), int):
            raise HarnessError("runtime tokenizer returned malformed token count")
        return int(payload["tokens"])


def _fill_words(rng_seed: int, count: int) -> str:
    words = [
        "amber",
        "brisk",
        "cedar",
        "delta",
        "ember",
        "fable",
        "glade",
        "harbor",
        "ivory",
        "juniper",
    ]
    return " ".join(
        f"{words[(rng_seed + i) % len(words)]}-{(rng_seed * 37 + i) % 997:03d}"
        for i in range(count)
    )


def generate_fixture(
    tier: str, *, seed: int = 1566, tokenizer: Any | None = None
) -> dict[str, Any]:
    if tier not in TIERS:
        raise HarnessError(f"unsupported fixture tier: {tier}")
    tokenizer = tokenizer or WhitespaceTokenizer()
    requested = TIERS[tier]
    targets = {
        "VII": "They were obliged to camp",
        "XIV": "You will remember there was",
        "XXI": "After climbing down from the",
        "canary": f"lunar-maple-{seed * 313 % 900000 + 100000}",
        "needle": f"needle-{tier}-{seed}",
    }
    sections: list[str] = [
        "P8 Synthetic Semantic Haystack",
        "Return JSON only with exactly these keys: VII, XIV, XXI, canary, needle.",
        "Each prose answer must be exactly five whitespace-separated words, preserve capitalization, and omit trailing punctuation.",
        "Table of Contents: VII They were obliged to camp out; XIV The Winged Monkeys; XXI The Lion Becomes the King.",
        "Decoy heading VII: They were obliged to camp out.",
    ]
    target_positions: dict[str, int] = {}
    slots = {k: int(requested * v) for k, v in TARGET_DEPTHS.items()}
    for idx in range(requested // 80 + 20):
        current = len(" ".join(sections).split())
        if "VII" not in target_positions and current >= slots["early"]:
            target_positions["VII"] = current
            sections.append("Chapter VII heading: They were obliged to camp out")
            sections.append(
                f"First prose sentence: {targets['VII']} before dawn without punctuation"
            )
        if "XIV" not in target_positions and current >= slots["middle"]:
            target_positions["XIV"] = current
            sections.append("Chapter XIV heading: The Winged Monkeys")
            sections.append(f"Opening prose: {targets['XIV']} a silver gate nearby")
        if "XXI" not in target_positions and current >= slots["late"]:
            target_positions["XXI"] = current
            sections.append("Chapter XXI heading: The Lion Becomes the King")
            sections.append(f"Opening prose: {targets['XXI']} mossy wall carefully")
            sections.append(f"Exact canary line: {targets['canary']}")
            sections.append(f"Single needle value: {targets['needle']}")
        sections.append(f"Decoy block {idx}: {_fill_words(seed + idx, 64)}")
        if len(" ".join(sections).split()) >= requested:
            break
    prompt = "\n".join(sections)
    actual = tokenizer.count(prompt)
    pad_idx = 0
    while actual < requested and pad_idx < 512:
        previous = actual
        sections.append(
            f"Padding block {pad_idx}: {_fill_words(seed + 5000 + pad_idx, min(256, requested - actual))}"
        )
        prompt = "\n".join(sections)
        actual = tokenizer.count(prompt)
        pad_idx += 1
        if (
            actual <= previous
            and getattr(tokenizer, "source", "") != WhitespaceTokenizer.source
        ):
            break
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    manifest = {
        "fixture_version": FIXTURE_VERSION,
        "fixture_id": tier,
        "seed": seed,
        "fixture_sha256": digest,
        "requested_tokens": requested,
        "actual_tokens": actual,
        "tokenizer_source": getattr(tokenizer, "source", "unknown"),
        "target_depths": target_positions,
        "expected": targets,
        "required_keys": ["VII", "XIV", "XXI", "canary", "needle"],
        "scoring_rules": {
            "json_only": True,
            "prose_word_count": 5,
            "preserve_capitalization": True,
            "no_trailing_punctuation": True,
        },
    }
    return {"prompt": prompt, "manifest": manifest}


def evaluate_semantic(response: str, manifest: dict[str, Any]) -> dict[str, Any]:
    stripped = response.strip()
    categories = {
        k: False
        for k in [
            "valid_json_only",
            "exact_key_set",
            "canary",
            "target_selection",
            "prose_not_heading",
            "word_count",
            "capitalization",
            "trailing_punctuation",
            "exact_match",
        ]
    }
    errors: list[str] = []
    if stripped.startswith("```") or stripped != response:
        errors.append("markdown_or_commentary")
    try:
        data = json.loads(stripped)
        categories["valid_json_only"] = isinstance(data, dict) and not errors
    except json.JSONDecodeError:
        errors.append("invalid_json")
        return {"semantic_pass": False, "categories": categories, "errors": errors}
    required = manifest["required_keys"]
    expected = manifest["expected"]
    categories["exact_key_set"] = set(data) == set(required)
    if not categories["exact_key_set"]:
        errors.append("key_set_mismatch")
    categories["canary"] = data.get("canary") == expected.get("canary")
    if not categories["canary"]:
        errors.append("canary_mismatch")
    prose_keys = [k for k in required if k not in {"canary", "needle"}]
    exacts = [data.get(k) == expected.get(k) for k in required]
    categories["target_selection"] = all(
        data.get(k) == expected.get(k) for k in prose_keys
    ) and data.get("needle") == expected.get("needle")
    headings = {
        "The Winged Monkeys",
        "The Lion Becomes the King",
        "They were obliged to camp out",
    }
    categories["prose_not_heading"] = all(
        data.get(k) not in headings for k in prose_keys
    )
    categories["word_count"] = all(
        isinstance(data.get(k), str) and len(data[k].split()) == 5 for k in prose_keys
    )
    categories["capitalization"] = all(
        data.get(k) == expected.get(k)
        or data.get(k, "")[:1].isupper() == expected.get(k, "")[:1].isupper()
        for k in prose_keys
    )
    categories["trailing_punctuation"] = all(
        isinstance(data.get(k), str)
        and not data[k].endswith((".", ",", ";", ":", "!", "?"))
        for k in prose_keys
    )
    for name, ok in categories.items():
        if (
            name != "exact_match"
            and not ok
            and name not in {"valid_json_only", "exact_key_set"}
        ):
            errors.append(f"{name}_failed")
    categories["exact_match"] = (
        categories["valid_json_only"] and categories["exact_key_set"] and all(exacts)
    )
    return {
        "semantic_pass": categories["exact_match"],
        "categories": categories,
        "errors": sorted(set(errors)),
    }


def score_trials(responses: Iterable[str], manifest: dict[str, Any]) -> dict[str, Any]:
    trials = [evaluate_semantic(r, manifest) for r in responses]
    exact = sum(1 for t in trials if t["semantic_pass"])
    return {
        "trial_count": len(trials),
        "exact_match_count": exact,
        "pass_rate": exact / len(trials) if trials else 0.0,
        "trials": trials,
    }


def validate_progress(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        raise HarnessError("missing progress telemetry")
    last_seq = -1
    last_processed = -1
    last_generated = -1
    total = None
    terminal = False
    last_phase_i = -1
    for ev in events:
        for key in ("seq", "phase"):
            if key not in ev:
                raise HarnessError(f"missing progress field: {key}")
        if terminal:
            raise HarnessError("progress_after_terminal")
        seq = int(ev["seq"])
        if seq <= last_seq:
            raise HarnessError("decreasing_sequence")
        last_seq = seq
        phase = str(ev["phase"])
        if phase not in PHASES:
            raise HarnessError("invalid_phase")
        phase_i = PHASES.index(phase)
        if phase_i < last_phase_i and phase not in {"failed", "cancelled"}:
            raise HarnessError("invalid_phase_transition")
        last_phase_i = phase_i
        processed = int(
            ev.get("processed_tokens", last_processed if last_processed >= 0 else 0)
        )
        generated = int(
            ev.get("generated_tokens", last_generated if last_generated >= 0 else 0)
        )
        if processed < last_processed:
            raise HarnessError("decreasing_processed")
        if generated < last_generated:
            raise HarnessError("decreasing_generated")
        last_processed, last_generated = processed, generated
        if "total_prompt_tokens" in ev:
            ev_total = int(ev["total_prompt_tokens"])
            if total is None:
                total = ev_total
            elif total != ev_total:
                raise HarnessError("changing_prompt_total")
            if processed > total:
                raise HarnessError("processed_exceeds_total")
        terminal = phase in {"completed", "cancelled", "failed"}
    return {
        "event_count": len(events),
        "first": events[0],
        "final": events[-1],
        "monotonic": True,
        "total_prompt_tokens": total,
    }


def calculate_metrics(
    events: list[dict[str, Any]],
    *,
    actual_output_tokens: int,
    request_budget_seconds: float,
) -> dict[str, Any]:
    progress = validate_progress(events)
    by_phase: dict[str, list[float]] = {}
    for ev in events:
        if "t" not in ev:
            raise HarnessError("missing timing telemetry")
        by_phase.setdefault(str(ev["phase"]), []).append(float(ev["t"]))
    start = min(float(e["t"]) for e in events)
    end = max(float(e["t"]) for e in events)
    total = end - start
    prefill = (
        (max(by_phase.get("prefill", [start])) - min(by_phase.get("prefill", [start])))
        if "prefill" in by_phase
        else None
    )
    decode = (
        (
            max(by_phase.get("generation", [end]))
            - min(by_phase.get("generation", [end]))
        )
        if "generation" in by_phase
        else None
    )
    prompt_tokens = progress.get("total_prompt_tokens") or 0
    return {
        "progress": progress,
        "durations_seconds": {"total": total, "prefill": prefill, "decode": decode},
        "throughput": {
            "prompt_tokens_per_second": prompt_tokens / prefill if prefill else None,
            "decode_tokens_per_second": (
                actual_output_tokens / decode if decode else None
            ),
        },
        "request_budget_seconds": request_budget_seconds,
        "remaining_margin_seconds": request_budget_seconds - total,
    }


def compare_kv_estimate(
    estimate: dict[str, Any],
    runtime: dict[str, Any],
    *,
    tolerance_bytes: int = 4096,
    require_exact: bool = True,
) -> dict[str, Any]:
    est = estimate.get("exact_kv_allocation_bytes") or estimate.get(
        "exact_kv_cache_bytes"
    )
    observed = runtime.get("kv_allocation_bytes")
    if require_exact and (
        not est or not observed or estimate.get("conservative_fallback_used")
    ):
        raise HarnessError("exact_kv_comparison_unavailable")
    delta = abs(int(est) - int(observed))
    return {
        "estimated_exact_kv_allocation_bytes": est,
        "runtime_kv_allocation_bytes": observed,
        "delta_bytes": delta,
        "tolerance_bytes": tolerance_bytes,
        "pass": delta <= tolerance_bytes,
        "alignment_rule": "llama.cpp reported KV buffer bytes must match P7 exact allocation within backend log rounding tolerance",
    }


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        s = value
        for pat in SECRET_PATTERNS:
            s = pat.sub("<redacted>", s)
        return s[:512]
    if isinstance(value, dict):
        return {
            str(k): sanitize(v)
            for k, v in value.items()
            if str(k)
            not in {
                "prompt",
                "response",
                "ciphertext",
                "iv",
                "key",
                "cancel_token",
                "request_id",
                "session_id",
                "client_id",
            }
        }
    if isinstance(value, list):
        return [sanitize(v) for v in value[:50]]
    return value


def atomic_write_report(
    report: dict[str, Any], out_dir: Path, name: str = "p8-report.json"
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = sanitize(report)
    safe["schema_version"] = SCHEMA_VERSION
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as h:
        json.dump(safe, h, indent=2, sort_keys=True)
        h.write("\n")
    dest = out_dir / name
    os.replace(tmp, dest)
    return dest


def validate_platform_backend(system: str, backend: str) -> dict[str, Any]:
    mapping = {"Darwin": {"metal", "cpu"}, "Windows": {"cuda", "cpu"}, "Linux": {"cpu"}}
    ok = backend in mapping.get(system, set())
    return {
        "platform": system,
        "backend_requested": backend,
        "supported": ok,
        "available_backends": sorted(mapping.get(system, [])),
    }


class FakePackagedAdapter:
    def __init__(
        self,
        response: str,
        events: list[dict[str, Any]],
        diagnostics: dict[str, Any] | None = None,
    ):
        self.response = response
        self.events = events
        self.diagnostics = diagnostics or {}
        self.cancelled = False

    def run(self, prompt: str, settings: dict[str, Any]) -> dict[str, Any]:
        return {
            "response": self.response,
            "progress_events": self.events,
            "diagnostics": self.diagnostics,
            "settings": settings,
        }

    def cancel_at(self, phase: str, threshold: int) -> dict[str, Any]:
        self.cancelled = True
        return {
            "acknowledged": True,
            "terminal_state": "cancelled",
            "cleanup_seconds": 0.01,
            "followup_succeeded": True,
            "operator_restart_succeeded": True,
            "trigger": {"phase": phase, "threshold": threshold},
        }


def run_report_only(
    adapter: Any, fixture: dict[str, Any], *, strict: bool, out_dir: Path
) -> int:
    result = adapter.run(fixture["prompt"], {"temperature": 0})
    semantic = evaluate_semantic(result["response"], fixture["manifest"])
    metrics = calculate_metrics(
        result["progress_events"],
        actual_output_tokens=len(result["response"].split()),
        request_budget_seconds=480,
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at_unix": int(time.time()),
        "fixture": fixture["manifest"],
        "semantic": semantic,
        "metrics": metrics,
        "runtime": sanitize(result.get("diagnostics", {})),
        "mode": "strict" if strict else "report-only",
    }
    path = atomic_write_report(report, out_dir)
    print(
        json.dumps(
            {
                "summary_version": SUMMARY_VERSION,
                "report": str(path),
                "semantic_pass": semantic["semantic_pass"],
                "actual_tokens": fixture["manifest"]["actual_tokens"],
            },
            sort_keys=True,
        )
    )
    return 1 if strict and not semantic["semantic_pass"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P8 packaged-runtime benchmark harness"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate-fixture")
    gen.add_argument("--tier", required=True, choices=sorted(TIERS))
    gen.add_argument("--out-dir", required=True)
    gen.add_argument("--seed", type=int, default=1566)
    ev = sub.add_parser("evaluate")
    ev.add_argument("--manifest", required=True)
    ev.add_argument("--response", required=True)
    ev.add_argument("--strict", action="store_true")
    run = sub.add_parser("run-packaged")
    run.add_argument("--app-binary")
    run.add_argument("--model")
    run.add_argument("--out-dir", required=True)
    run.add_argument("--backend", default="metal")
    args = parser.parse_args(argv)
    if args.cmd == "generate-fixture":
        fx = generate_fixture(args.tier, seed=args.seed)
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{args.tier}.prompt.txt").write_text(fx["prompt"], encoding="utf-8")
        atomic_write_report(fx["manifest"], out, f"{args.tier}.manifest.json")
        print(
            json.dumps(
                {
                    "fixture_id": args.tier,
                    "actual_tokens": fx["manifest"]["actual_tokens"],
                    "target_depths": fx["manifest"]["target_depths"],
                },
                sort_keys=True,
            )
        )
        return 0
    if args.cmd == "evaluate":
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        response = Path(args.response).read_text(encoding="utf-8")
        result = evaluate_semantic(response, manifest)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1 if args.strict and not result["semantic_pass"] else 0
    if args.cmd == "run-packaged":
        if not args.app_binary or not args.model:
            parser.error(
                "packaged-runtime mode requires --app-binary and --model; fake adapters are forbidden"
            )
        if not Path(args.app_binary).exists() or not Path(args.model).exists():
            raise HarnessError("packaged-runtime prerequisites are absent")
        support = validate_platform_backend(platform.system(), args.backend)
        if not support["supported"]:
            raise HarnessError(f"unsupported platform/backend: {support}")
        raise HarnessError(
            "real packaged adapter is intentionally prerequisite-gated; use desktop packaged app integration hooks on hardware"
        )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
