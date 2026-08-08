#!/usr/bin/env python3
"""P8 packaged-runtime benchmark harness utilities and CLI.

The default unit-testable paths use deterministic adapters only. The explicit
``run --runtime packaged`` mode validates prerequisites and fails closed unless a
real packaged desktop bridge is supplied.
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
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "p8-benchmark-report/v1"
FIXTURE_VERSION = "p8-synthetic-fixture/v1"
EXIT_OK = 0
EXIT_STRICT_FAILURE = 2
EXIT_INPUT_FAILURE = 3
EXIT_RUNTIME_UNAVAILABLE = 4

CONTEXT_TARGETS = {"small-8k": 8192, "intermediate-32k": 32768, "long-55k": 55254}
DEPTHS = {"early": 0.12, "middle": 0.50, "late": 0.86}
EXPECTED_KEYS = ["VII", "XIV", "XXI", "canary"]
EXPECTED = {
    "VII": "They were obliged to camp",
    "XIV": "You will remember there was",
    "XXI": "After climbing down from the",
    "canary": "lunar-maple-508163",
}
TARGET_CHAPTER = {"VII": "VII", "XIV": "XIV", "XXI": "XXI"}
TITLE_DECOYS = {"XIV": "The Winged Monkeys", "XXI": "The Lion Becomes the King"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def simple_token_count(text: str) -> int:
    """Deterministic fallback tokenizer used only when authoritative runtime is absent."""
    return len(re.findall(r"\S+", text))


class RuntimeTokenizer:
    def count(self, text: str) -> int:  # pragma: no cover - interface
        raise NotImplementedError


class FallbackTokenizer(RuntimeTokenizer):
    authoritative = False

    def count(self, text: str) -> int:
        return simple_token_count(text)


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    prompt: str
    manifest: dict[str, Any]


def _pad_words(count: int, seed: str, segment: str) -> str:
    return " ".join(f"hay-{seed}-{segment}-{i:05d}" for i in range(count))


def generate_fixture(size: str, *, seed: str = "p8-default", tokenizer: RuntimeTokenizer | None = None) -> Fixture:
    if size not in CONTEXT_TARGETS:
        raise ValueError(f"unknown fixture size: {size}")
    tokenizer = tokenizer or FallbackTokenizer()
    requested = CONTEXT_TARGETS[size]
    canary = EXPECTED["canary"]
    prelude = (
        "Return JSON only. Use exactly the keys VII, XIV, XXI, and canary. "
        "For each roman numeral key, return the first five prose words from that chapter, preserving capitalization, "
        "without trailing punctuation. Do not return headings or table-of-contents entries. "
        "For canary, return the exact canary token.\n\n"
        "TABLE OF CONTENTS\nVII They were obliged to camp out\nXIV The Winged Monkeys\nXXI The Lion Becomes the King\n\n"
    )
    targets = {
        "VII": "CHAPTER VII\nHeading: They were obliged to camp out\nThey were obliged to camp under a glass-green ridge.\n",
        "XIV": "CHAPTER XIV\nHeading: The Winged Monkeys\nYou will remember there was a brass gate beside the orchard.\n",
        "XXI": "CHAPTER XXI\nHeading: The Lion Becomes the King\nAfter climbing down from the bright ladder, the travelers counted stones.\n",
    }
    target_tokens = {k: max(10, int(requested * DEPTHS[d])) for k, d in zip(EXPECTED_KEYS[:3], DEPTHS)}
    parts: list[str] = [prelude]
    current = tokenizer.count("".join(parts))
    placements: dict[str, dict[str, Any]] = {}
    for key in ("VII", "XIV", "XXI"):
        need = max(0, target_tokens[key] - current)
        parts.append(_pad_words(need, seed, key.lower()) + "\n")
        current = tokenizer.count("".join(parts))
        placements[key] = {"target_depth_label": next(k for k, v in zip(DEPTHS, EXPECTED_KEYS[:3]) if v == key), "token_offset_before_target": current}
        parts.append(targets[key])
        current = tokenizer.count("".join(parts))
    parts.append(f"CANARY RECORD: {canary}\n")
    current = tokenizer.count("".join(parts))
    remaining = max(0, requested - current)
    parts.append(_pad_words(remaining, seed, "tail"))
    prompt = "".join(parts)
    actual = tokenizer.count(prompt)
    fixture_hash = hashlib.sha256(prompt.encode()).hexdigest()
    manifest = {
        "fixture_version": FIXTURE_VERSION,
        "fixture_id": f"synthetic-{size}",
        "deterministic_seed": seed,
        "fixture_sha256": fixture_hash,
        "requested_token_count": requested,
        "actual_token_count": actual,
        "tokenizer_authoritative": bool(getattr(tokenizer, "authoritative", False)),
        "target_depths": placements,
        "expected_answers": EXPECTED,
        "expected_key_set": EXPECTED_KEYS,
        "scoring_rules": ["json_only", "exact_key_set", "canary", "chapter_selection", "prose_not_heading", "word_count", "capitalization", "trailing_punctuation", "exact_match"],
    }
    return Fixture(f"synthetic-{size}", prompt, manifest)


def validate_manifest(fixture: Fixture) -> None:
    if hashlib.sha256(fixture.prompt.encode()).hexdigest() != fixture.manifest.get("fixture_sha256"):
        raise ValueError("fixture hash mismatch")
    if fixture.manifest.get("expected_answers") != EXPECTED:
        raise ValueError("oracle mismatch")
    if fixture.manifest.get("expected_key_set") != EXPECTED_KEYS:
        raise ValueError("key-set mismatch")


def evaluate_semantic(response: str, manifest: dict[str, Any]) -> dict[str, Any]:
    stripped = response.strip()
    categories = {k: False for k in ["valid_json_only", "exact_key_set", "canary", "chapter_selection", "prose_not_heading", "word_count", "capitalization", "trailing_punctuation", "exact_match"]}
    if stripped != response or stripped.startswith("```") or not stripped.startswith("{"):
        return {"semantic_pass": False, "categories": categories, "error_code": "not_json_only"}
    if not stripped.endswith("}") and "}" in stripped:
        return {"semantic_pass": False, "categories": categories, "error_code": "not_json_only"}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return {"semantic_pass": False, "categories": categories, "error_code": "invalid_json"}
    categories["valid_json_only"] = True
    expected = manifest["expected_answers"]
    keys = manifest["expected_key_set"]
    categories["exact_key_set"] = list(parsed.keys()) == keys
    categories["canary"] = parsed.get("canary") == expected["canary"]
    chapter_ok = True
    prose_ok = True
    wc_ok = True
    cap_ok = True
    punct_ok = True
    exact_ok = True
    failures: list[str] = []
    for key in keys:
        got = parsed.get(key)
        exp = expected[key]
        if got != exp:
            exact_ok = False
            failures.append(key)
        if key in TARGET_CHAPTER:
            if got in TITLE_DECOYS.values() or got == TITLE_DECOYS.get(key):
                chapter_ok = False
                prose_ok = False
            if got != exp:
                chapter_ok = False
            if isinstance(got, str):
                wc_ok = wc_ok and len(got.split()) == len(exp.split())
                cap_ok = cap_ok and (got[:1] == exp[:1])
                punct_ok = punct_ok and not re.search(r"[.!?,;:]$", got)
            else:
                wc_ok = cap_ok = punct_ok = False
    categories.update({"chapter_selection": chapter_ok, "prose_not_heading": prose_ok, "word_count": wc_ok, "capitalization": cap_ok, "trailing_punctuation": punct_ok, "exact_match": exact_ok})
    return {"semantic_pass": all(categories.values()), "categories": categories, "failure_keys": failures}


def score_trials(responses: list[str], manifest: dict[str, Any]) -> dict[str, Any]:
    trials = [evaluate_semantic(r, manifest) for r in responses]
    exact = sum(1 for t in trials if t["categories"].get("exact_match"))
    aggregate: dict[str, int] = {k: 0 for k in trials[0]["categories"]} if trials else {}
    for trial in trials:
        for k, v in trial["categories"].items():
            aggregate[k] += int(bool(v))
    return {"trial_count": len(trials), "exact_match_count": exact, "pass_rate": exact / len(trials) if trials else 0.0, "category_pass_counts": aggregate, "trials": trials}


def assert_progress_invariants(events: list[dict[str, Any]]) -> dict[str, Any]:
    last_seq = -1; last_processed = -1; last_generated = -1; total = None; terminal = False; phase_index = -1
    order = {"preparing": 0, "prefill": 1, "generation": 2, "completed": 3, "canceled": 3, "error": 3}
    for ev in events:
        phase = ev.get("phase") or ev.get("type")
        if terminal:
            raise ValueError("progress_after_terminal")
        seq = ev.get("sequence")
        if not isinstance(seq, int) or seq <= last_seq: raise ValueError("decreasing_sequence")
        last_seq = seq
        if phase not in order or order[phase] < phase_index: raise ValueError("invalid_phase_transition")
        phase_index = order[phase]
        if "prompt_total_tokens" in ev:
            if total is None: total = ev["prompt_total_tokens"]
            elif ev["prompt_total_tokens"] != total: raise ValueError("changing_prompt_total")
        p = ev.get("processed_tokens", last_processed)
        g = ev.get("generated_tokens", last_generated)
        if p < last_processed: raise ValueError("decreasing_processed")
        if g < last_generated: raise ValueError("decreasing_generated")
        if total is not None and p > total: raise ValueError("processed_exceeds_total")
        last_processed, last_generated = p, g
        terminal = phase in {"completed", "canceled", "error"}
    return {"progress_event_count": len(events), "first_progress": events[0] if events else None, "final_progress": events[-1] if events else None, "monotonic": True, "total_consistent": True, "processed_never_exceeds_total": True}


def phase_metrics(events: list[dict[str, Any]], total_duration_seconds: float, request_budget_seconds: float) -> dict[str, Any]:
    progress = assert_progress_invariants(events)
    first_prefill = next((e for e in events if (e.get("phase") or e.get("type")) == "prefill"), None)
    first_gen = next((e for e in events if (e.get("phase") or e.get("type")) == "generation"), None)
    final = events[-1] if events else {}
    prompt_tokens = int(final.get("processed_tokens") or 0)
    output_tokens = int(final.get("generated_tokens") or 0)
    prefill_s = float((first_gen or final).get("elapsed_seconds", 0) - (first_prefill or {"elapsed_seconds": 0}).get("elapsed_seconds", 0))
    decode_s = max(0.0, total_duration_seconds - float((first_gen or {"elapsed_seconds": total_duration_seconds}).get("elapsed_seconds", total_duration_seconds)))
    return {**progress, "prefill_duration_seconds": prefill_s, "decode_duration_seconds": decode_s, "total_duration_seconds": total_duration_seconds, "prompt_tokens_per_second": prompt_tokens / prefill_s if prefill_s > 0 else None, "decode_tokens_per_second": output_tokens / decode_s if decode_s > 0 else None, "request_budget_seconds": request_budget_seconds, "remaining_completion_margin_seconds": request_budget_seconds - total_duration_seconds}


def compare_kv_estimate(estimate: dict[str, Any], observed: dict[str, Any], *, require_exact: bool = True, tolerance_bytes: int = 0) -> dict[str, Any]:
    exact = estimate.get("exact_kv_allocation_bytes")
    runtime = observed.get("kv_allocation_bytes")
    if require_exact and (not exact or estimate.get("conservative_fallback_used") or not runtime):
        raise ValueError("exact_kv_comparison_unavailable")
    delta = None if exact is None or runtime is None else int(runtime) - int(exact)
    return {"estimated_exact_kv_allocation_bytes": exact, "runtime_kv_allocation_bytes": runtime, "delta_bytes": delta, "tolerance_bytes": tolerance_bytes, "comparison_pass": delta is not None and abs(delta) <= tolerance_bytes, "alignment_rule": "runtime GGML KV buffer allocation must equal exact estimator allocation within configured byte tolerance"}


def sanitize_report(obj: Any) -> Any:
    secret_re = re.compile(r"(token|secret|ciphertext|\biv\b|/Users/|/home/|[A-Za-z]:\\)", re.I)
    path_key_re = re.compile(r"(^|_)(path|file|dir)$", re.I)
    if isinstance(obj, dict):
        return {str(k): sanitize_report(v) for k, v in obj.items() if not (path_key_re.search(str(k)) and isinstance(v, str) and secret_re.search(v))}
    if isinstance(obj, list): return [sanitize_report(v) for v in obj]
    if isinstance(obj, str): return "<redacted>" if secret_re.search(obj) else obj[:500]
    return obj


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = sanitize_report(payload)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(safe, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        name = tmp.name
    os.replace(name, path)


def validate_packaged_inputs(args: argparse.Namespace) -> Path:
    bridge = Path(args.bridge or "")
    if not bridge.is_file():
        raise FileNotFoundError("packaged runtime bridge is required; no fake runtime will be substituted")
    if args.model and not Path(args.model).is_file():
        raise FileNotFoundError("model artifact is required for packaged runtime")
    return bridge



def probe_platform_memory(adapter: Callable[[], dict[str, Any]], *, timeout_seconds: float = 2.0) -> dict[str, Any]:
    start = time.monotonic()
    try:
        payload = adapter()
    except TimeoutError:
        return {"available": False, "error_code": "timeout", "methodology": "adapter"}
    except FileNotFoundError:
        return {"available": False, "error_code": "probe_absent", "methodology": "adapter"}
    if time.monotonic() - start > timeout_seconds:
        return {"available": False, "error_code": "timeout", "methodology": "adapter"}
    if not isinstance(payload, dict):
        return {"available": False, "error_code": "malformed_output", "methodology": "adapter"}
    return {"available": True, "methodology": "adapter", "payload": sanitize_report(payload)}


def validate_cancellation_scenario(events: list[dict[str, Any]], *, trigger_phase: str, followup_ok: bool, cleanup_seconds: float, cleanup_budget_seconds: float) -> dict[str, Any]:
    cancel_seen = False
    terminal_seen = False
    for ev in events:
        phase = ev.get("phase") or ev.get("type")
        if phase == "cancel_requested" and ev.get("trigger_phase") == trigger_phase:
            cancel_seen = True
        if phase == "completed" and cancel_seen:
            raise ValueError("late_result_after_cancellation")
        if phase == "canceled":
            terminal_seen = True
        elif terminal_seen:
            raise ValueError("stale_progress_after_cancellation")
    if not cancel_seen:
        raise ValueError("cancellation_not_triggered")
    if not terminal_seen:
        raise ValueError("cancellation_not_acknowledged")
    if cleanup_seconds > cleanup_budget_seconds:
        raise ValueError("cancellation_cleanup_timeout")
    if not followup_ok:
        raise ValueError("clean_worker_followup_failed")
    return {"cancellation_acknowledged": True, "cleanup_seconds": cleanup_seconds, "followup_request_succeeded": True, "operator_stop_start_functional": True}

def command_generate(args: argparse.Namespace) -> int:
    fx = generate_fixture(args.size, seed=args.seed)
    validate_manifest(fx)
    out = Path(args.output_dir)
    atomic_write_json(out / f"{fx.fixture_id}.manifest.json", fx.manifest)
    (out / f"{fx.fixture_id}.prompt.txt").write_text(fx.prompt, encoding="utf-8")
    print(f"{fx.fixture_id}: requested={fx.manifest['requested_token_count']} actual={fx.manifest['actual_token_count']} sha256={fx.manifest['fixture_sha256']}")
    return EXIT_OK


def command_eval(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text())
    response = Path(args.response).read_text()
    result = evaluate_semantic(response, manifest)
    report = {"schema_version": SCHEMA_VERSION, "mode": "semantic-eval", "semantic": result, "fixture": {k: manifest[k] for k in ("fixture_version", "fixture_id", "fixture_sha256", "requested_token_count", "actual_token_count", "target_depths")}}
    atomic_write_json(Path(args.output_dir) / "p8-semantic-report.json", report)
    print(f"semantic_pass={result['semantic_pass']} categories={result['categories']}")
    return EXIT_OK if result["semantic_pass"] or args.report_only else EXIT_STRICT_FAILURE


def command_run(args: argparse.Namespace) -> int:
    if args.runtime != "packaged":
        raise ValueError("only --runtime packaged is supported for real benchmark execution")
    bridge = validate_packaged_inputs(args)
    # Real packaged mode is intentionally explicit and fail-closed. The bridge invocation is a bounded
    # identity/preflight probe here; long inference orchestration is documented and adapters are unit-tested.
    start = time.monotonic()
    proc = subprocess.run([sys.executable, str(bridge), "--help"], cwd=bridge.parent, text=True, capture_output=True, timeout=args.timeout_seconds)  # noqa: S603
    elapsed = time.monotonic() - start
    report = {"schema_version": SCHEMA_VERSION, "mode": "packaged-preflight", "runtime": {"platform": platform.system().lower(), "bridge_basename": bridge.name, "returncode": proc.returncode}, "metrics": {"total_duration_seconds": elapsed}, "semantic": {"semantic_pass": None, "mode": "not_run_preflight_only"}, "hardware_validation": {"metal_cuda_inference_completed": False}}
    atomic_write_json(Path(args.output_dir) / "p8-packaged-runtime-report.json", report)
    print(f"packaged preflight returncode={proc.returncode} elapsed={elapsed:.3f}s report={args.output_dir}")
    return EXIT_OK if proc.returncode == 0 else EXIT_RUNTIME_UNAVAILABLE


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="P8 packaged runtime benchmark harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate-fixture"); g.add_argument("--size", choices=sorted(CONTEXT_TARGETS), required=True); g.add_argument("--seed", default="p8-default"); g.add_argument("--output-dir", required=True); g.set_defaults(func=command_generate)
    e = sub.add_parser("eval-response"); e.add_argument("--manifest", required=True); e.add_argument("--response", required=True); e.add_argument("--output-dir", required=True); e.add_argument("--report-only", action="store_true"); e.set_defaults(func=command_eval)
    r = sub.add_parser("run"); r.add_argument("--runtime", choices=["packaged"], required=True); r.add_argument("--bridge"); r.add_argument("--model"); r.add_argument("--output-dir", required=True); r.add_argument("--timeout-seconds", type=float, default=30); r.set_defaults(func=command_run)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except (ValueError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"p8_benchmark_error={type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INPUT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
