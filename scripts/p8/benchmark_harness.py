"""P8 packaged-runtime benchmark harness utilities.

The module is intentionally dependency-light so ordinary CI can validate fixture,
semantic, progress, cancellation, memory-comparison, and report contracts without
model downloads, GPUs, or a packaged desktop app. The ``packaged-runtime`` CLI
mode invokes the repository-owned packaged desktop WebDriver runner.
"""
from __future__ import annotations

import argparse
from collections import deque
import contextlib
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import math
import signal
import threading
import time
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from utils.context_profiles import get_context_profile

SCHEMA_VERSION = "p8-benchmark-report-v1"
FIXTURE_VERSION = "p8-semantic-haystack-v3"
DEFAULT_SEED = "p8-1566"
PHASES = {"preparing": 0, "prefill": 1, "generating": 2}
SECRET_PATTERNS = [
    re.compile(r"(?i)\b(authorization|api[_-]?key|secret|token)\b\s*[:=]\s*(?:bearer\s+)?[^\s,}\]\)]+"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
    re.compile(r"/Users/[^/\s]+"),
    re.compile(r"/home/[^/\s]+"),
]
SENSITIVE_KEYS = {
    "prompt", "prompts", "response", "responses", "response_text", "content",
    "message", "messages", "tool_argument", "tool_arguments", "model_output",
    "completion", "completions", "plaintext", "ciphertext", "iv", "key",
    "authorization", "api_key", "apikey", "secret", "token", "cancel_token",
    "request_id", "client_id", "session_id",
}

@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    requested_tokens: int
    approximate: bool = False

FIXTURES = {
    "small-8k": FixtureSpec("small-8k", 8192),
    "intermediate-32k": FixtureSpec("intermediate-32k", 32768),
    "long-55k": FixtureSpec("long-55k", 55254, True),
}

def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def _count_tokens(text: str, tokenizer: Callable[[str], int] | None = None) -> int:
    if tokenizer:
        return int(tokenizer(text))
    return len(text.split())


def _read_bounded_text(path: str, limit: int = 4 * 1024 * 1024) -> str:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = handle.read(limit + 1)
    if len(value.encode("utf-8")) > limit:
        raise ValueError("fixture_too_large")
    return value


def _validate_authoritative_tokenizer_evidence(evidence: Any, manifest: dict[str, Any],
        runtime_identity: str, total_prompt_tokens: int) -> tuple[dict[str, int] | None, str | None]:
    """Validate counts produced by the packaged admission tokenizer, never fixture estimates."""
    if not isinstance(evidence, dict) or evidence.get("method") != "packaged_admission_render_and_tokenize_chat":
        return None, "authoritative_target_depth_malformed"
    if evidence.get("runtime_identity") != runtime_identity or evidence.get("total_prompt_tokens") != total_prompt_tokens:
        return None, "authoritative_target_depth_mismatched"
    offsets = evidence.get("target_offsets_tokens")
    if (not isinstance(offsets, dict) or set(offsets) != set(manifest["targets"])
            or not all(isinstance(value, int) and 0 <= value < total_prompt_tokens for value in offsets.values())):
        return None, "authoritative_target_depth_malformed"
    if len(set(offsets.values())) != len(offsets):
        return None, "authoritative_target_depth_ambiguous"
    return offsets, None

def generate_fixture(fixture_id: str, seed: str = DEFAULT_SEED,
        tokenizer: Callable[[str], int] | None = None,
        scenario: str = "structured-extraction") -> tuple[str, dict[str, Any]]:
    if scenario not in {"single-needle", "structured-extraction"}:
        raise ValueError("fixture_scenario_invalid")
    spec = FIXTURES[fixture_id]
    canary = "lunar-maple-508163"
    targets = {
        "VII": "They were obliged to camp",
        "XIV": "You will remember there was",
        "XXI": "After climbing down from the",
        "canary": canary,
        "needle": f"needle-{hashlib.sha256(f'{FIXTURE_VERSION}:{seed}:{fixture_id}'.encode()).hexdigest()[:16]}",
    }
    toc = "\n".join(["Table of Contents", "VII. They were obliged to camp out", "XIV. The Winged Monkeys", "XXI. The Lion Becomes the King"])
    needle = targets["needle"]
    if scenario == "single-needle":
        prompt_parts = [
            "You must answer with JSON only, no Markdown, no commentary, and exactly the key needle.",
            "Return the exact value on the single NEEDLE FACT line.",
        ]
        expected_answers = {"needle": needle}
        positions = {"needle": {"small-8k": 0.18, "intermediate-32k": 0.50,
            "long-55k": 0.82}[fixture_id]}
        semantic_oracle: dict[str, Any] = {"value_key": "needle"}
        scoring_rules = ["json_only", "exact_key_set", "needle_exact", "exact_match", "semantic_pass"]
    else:
        prompt_parts = [
        "You must answer with JSON only, no Markdown, no commentary, and exactly the keys VII, XIV, XXI, canary.",
        "For VII, XIV, and XXI return exactly the first five whitespace-separated words of the first prose sentence in that chapter, preserving capitalization and omitting trailing punctuation.",
        "For canary return the exact value on the single RECORD CANARY line.",
        toc,
        ]
        expected_answers = {"VII": targets["VII"], "XIV": targets["XIV"],
            "XXI": targets["XXI"], "canary": canary}
        positions = {"VII": 0.12, "XIV": 0.42, "XXI": 0.70, "canary": 0.90}
        semantic_oracle = {"prose_keys": ["VII", "XIV", "XXI"],
            "heading_decoys": ["They were obliged to camp out", "The Winged Monkeys",
                "The Lion Becomes the King"]}
        scoring_rules = ["json_only", "exact_key_set", "canary_exact", "target_selection",
            "prose_not_heading", "word_count", "capitalization", "trailing_punctuation",
            "exact_match", "semantic_pass"]
    chapter_sentences = {
        "VII": "They were obliged to camp beside the road before sunrise. This prose sentence is not the heading.",
        "XIV": "You will remember there was no road--not even a pathway--between the castle and the city. This prose sentence is not the title.",
        "XXI": "After climbing down from the China wall the travelers found themselves in a disagreeable country. This prose sentence is not the title.",
    }
    target_markers: dict[str, int] = {}
    filler_i = 0
    while True:
        joined = "\n".join(prompt_parts)
        cur = _count_tokens(joined, tokenizer)
        if cur >= spec.requested_tokens + 20:
            break
        ratio = cur / max(spec.requested_tokens, 1)
        inserted = False
        for chap, pos in positions.items():
            if chap not in target_markers and ratio >= pos:
                if chap in chapter_sentences:
                    addition = f"\nChapter {chap}: {toc.splitlines()[['VII','XIV','XXI'].index(chap)+1]}\n{chapter_sentences[chap]}"
                elif chap == "needle":
                    addition = f"NEEDLE FACT: {targets['needle']}"
                else:
                    addition = f"RECORD CANARY: {canary}"
                prefix = "\n".join(prompt_parts) + "\n" + addition.split(targets[chap], 1)[0]
                prompt_parts.append(addition)
                target_markers[chap] = _count_tokens(prefix, tokenizer)
                inserted = True
        if not inserted:
            decoy = hashlib.sha256(f"{seed}:{fixture_id}:{filler_i}".encode()).hexdigest()[:16]
            prompt_parts.append(f"Decoy paragraph {filler_i:05d} repeats chapter-title-like text but contains no answer. Similar marker needle-{decoy} is not the requested fact.")
            filler_i += 1
    for chap in (() if scenario == "single-needle" else ("VII", "XIV", "XXI")):
        if chap not in target_markers:
            target_markers[chap] = _count_tokens("\n".join(prompt_parts), tokenizer)
            prompt_parts.append(f"\nChapter {chap}: decoy heading\n{chapter_sentences[chap]}")
    prompt = "\n".join(prompt_parts).rstrip() + "\n"
    actual = _count_tokens(prompt, tokenizer)
    manifest = {
        "fixture_version": FIXTURE_VERSION, "fixture_id": fixture_id, "seed": seed,
        "scenario": scenario,
        "requested_tokens": spec.requested_tokens, "actual_tokens": actual, "tokenizer": "supplied-callback" if tokenizer else "whitespace-ci",
        "token_count_provenance": {"kind": "estimate", "tokenizer_id": "supplied-callback" if tokenizer else "whitespace-ci", "authoritative": False, "units": "tokens"},
        "fixture_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "target_depths_tokens": target_markers,
        "targets": {name: {"value": targets[name], "requested_offset_tokens": round(spec.requested_tokens * positions[name]),
            "requested_ratio": positions[name], "actual_offset_tokens": offset,
            "actual_ratio": offset / actual} for name, offset in target_markers.items()},
        "expected_answers": expected_answers, "semantic_oracle": semantic_oracle,
        "scoring_rules": scoring_rules,
    }
    return prompt, manifest


def validate_manifest(manifest: Any, prompt: str | None = None) -> dict[str, Any]:
    """Validate a fixture oracle before evaluation or an expensive packaged launch."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest_not_object")
    required = {"fixture_version", "fixture_id", "scenario", "seed", "requested_tokens",
        "actual_tokens", "fixture_sha256", "expected_answers", "scoring_rules",
        "token_count_provenance", "target_depths_tokens", "targets", "semantic_oracle"}
    if not required.issubset(manifest):
        raise ValueError("manifest_missing_fields")
    if manifest["fixture_version"] != FIXTURE_VERSION or manifest["fixture_id"] not in FIXTURES:
        raise ValueError("manifest_identity_invalid")
    if not isinstance(manifest["seed"], str) or not manifest["seed"] or not isinstance(manifest["scenario"], str):
        raise ValueError("manifest_identity_invalid")
    scenario = manifest["scenario"]
    if scenario not in {"single-needle", "structured-extraction"}:
        raise ValueError("manifest_identity_invalid")
    if (manifest["requested_tokens"] != FIXTURES[manifest["fixture_id"]].requested_tokens or
            not all(isinstance(manifest[key], int) and manifest[key] > 0 for key in ("requested_tokens", "actual_tokens"))):
        raise ValueError("manifest_token_counts_invalid")
    if not isinstance(manifest["fixture_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["fixture_sha256"]):
        raise ValueError("manifest_hash_invalid")
    expected = manifest["expected_answers"]
    expected_keys = {"needle"} if scenario == "single-needle" else {"VII", "XIV", "XXI", "canary"}
    if not isinstance(expected, dict) or set(expected) != expected_keys or not all(isinstance(v, str) and v for v in expected.values()):
        raise ValueError("manifest_oracle_invalid")
    expected_rules = (["json_only", "exact_key_set", "needle_exact", "exact_match", "semantic_pass"]
        if scenario == "single-needle" else
        ["json_only", "exact_key_set", "canary_exact", "target_selection", "prose_not_heading",
         "word_count", "capitalization", "trailing_punctuation", "exact_match", "semantic_pass"])
    if manifest["scoring_rules"] != expected_rules:
        raise ValueError("manifest_scoring_invalid")
    oracle = manifest["semantic_oracle"]
    if ((scenario == "single-needle" and oracle != {"value_key": "needle"}) or
            (scenario == "structured-extraction" and
             (not isinstance(oracle, dict) or oracle.get("prose_keys") != ["VII", "XIV", "XXI"]
              or not isinstance(oracle.get("heading_decoys"), list)
              or not all(isinstance(value, str) and value for value in oracle["heading_decoys"])))):
        raise ValueError("manifest_oracle_invalid")
    provenance = manifest["token_count_provenance"]
    if (not isinstance(provenance, dict) or provenance.get("kind") != "estimate" or
            provenance.get("authoritative") is not False or provenance.get("units") != "tokens" or
            not isinstance(provenance.get("tokenizer_id"), str)):
        raise ValueError("manifest_token_provenance_invalid")
    targets = manifest["targets"]
    if not isinstance(targets, dict) or set(targets) != expected_keys:
        raise ValueError("manifest_targets_invalid")
    offsets = []
    for metadata in targets.values():
        if (not isinstance(metadata, dict) or not isinstance(metadata.get("value"), str) or not metadata["value"]
                or not isinstance(metadata.get("requested_offset_tokens"), int)
                or not isinstance(metadata.get("actual_offset_tokens"), int)
                or not isinstance(metadata.get("requested_ratio"), (int, float))
                or not isinstance(metadata.get("actual_ratio"), (int, float))
                or not 0 <= metadata["actual_offset_tokens"] < manifest["actual_tokens"]
                or not 0 <= metadata["actual_ratio"] <= 1
                or metadata["actual_ratio"] != metadata["actual_offset_tokens"] / manifest["actual_tokens"]):
            raise ValueError("manifest_targets_invalid")
        offsets.append(metadata["actual_offset_tokens"])
    if any(targets[key]["value"] != expected[key] for key in expected):
        raise ValueError("manifest_targets_invalid")
    requested_order = sorted(targets, key=lambda name: targets[name]["requested_offset_tokens"])
    actual_order = sorted(targets, key=lambda name: targets[name]["actual_offset_tokens"])
    if requested_order != actual_order or len(set(offsets)) != len(offsets):
        raise ValueError("manifest_target_order_invalid")
    if manifest["target_depths_tokens"] != {
            name: metadata["actual_offset_tokens"] for name, metadata in targets.items()}:
        raise ValueError("manifest_targets_invalid")
    if prompt is not None:
        if len(prompt.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("fixture_too_large")
        if hashlib.sha256(prompt.encode()).hexdigest() != manifest["fixture_sha256"]:
            raise ValueError("fixture_hash_mismatch")
        unique_targets = targets.values() if scenario == "single-needle" else (targets["canary"],)
        if any(prompt.count(metadata["value"]) != 1 for metadata in unique_targets):
            raise ValueError("fixture_target_occurrence_invalid")
    return manifest

def evaluate_semantic(response_text: str, manifest: dict[str, Any]) -> dict[str, Any]:
    expected = manifest["expected_answers"]
    if manifest.get("scenario") == "single-needle":
        stripped = response_text.strip()
        try:
            parsed = json.loads(stripped)
            json_only = True
        except (TypeError, json.JSONDecodeError):
            parsed, json_only = None, False
        exact_keys = isinstance(parsed, dict) and set(parsed) == {"needle"}
        needle_exact = exact_keys and isinstance(parsed["needle"], str) and parsed["needle"] == expected["needle"]
        exact_match = parsed == expected
        result = {"json_only": json_only, "exact_key_set": exact_keys,
            "needle_exact": needle_exact, "exact_match": exact_match,
            "semantic_pass": exact_match}
        result["errors"] = [rule for rule in manifest["scoring_rules"] if not result[rule]]
        return result
    fields = ["json_only", "exact_key_set", "canary_exact", "target_selection", "prose_not_heading", "word_count", "capitalization", "trailing_punctuation", "exact_match", "semantic_pass"]
    result = {key: False for key in fields}
    parse_error: str | None = None
    stripped = response_text.strip()
    try:
        parsed = json.loads(stripped)
        result["json_only"] = True
    except (TypeError, json.JSONDecodeError):
        parsed = None
        parse_error = "invalid_json"
    if not isinstance(parsed, dict):
        result["errors"] = ([parse_error] if parse_error else []) + [key for key in fields if not result[key]]
        return result

    prose_keys = manifest.get("semantic_oracle", {}).get("prose_keys", [key for key in expected if key != "canary"])
    required_strings = all(isinstance(parsed.get(key), str) for key in expected)
    result["exact_key_set"] = set(parsed) == set(expected)
    result["canary_exact"] = parsed.get("canary") == expected.get("canary")

    def normalized(value: str) -> str:
        return " ".join(value.split()).rstrip(".,;:!?")

    heading_decoys = {
        normalized(value).casefold()
        for value in manifest.get("semantic_oracle", {}).get("heading_decoys", [])
        if isinstance(value, str)
    }
    if required_strings:
        prose_values = [parsed[key] for key in prose_keys]
        result["target_selection"] = all(normalized(parsed[key]).casefold() == normalized(expected[key]).casefold() for key in prose_keys)
        result["prose_not_heading"] = all(normalized(value).casefold() not in heading_decoys for value in prose_values)
        result["word_count"] = all(len(parsed[key].split()) == len(expected[key].split()) for key in prose_keys)
        result["capitalization"] = all(normalized(parsed[key]) == normalized(expected[key]) for key in prose_keys)
        result["trailing_punctuation"] = all(not parsed[key].rstrip().endswith(tuple(".,;:!?")) for key in prose_keys)

    result["exact_match"] = result["exact_key_set"] and parsed == expected
    result["semantic_pass"] = result["exact_match"]
    result["errors"] = [key for key in fields if not result[key]]
    return result

def score_trials(responses: list[str], manifest: dict[str, Any]) -> dict[str, Any]:
    trials = [evaluate_semantic(r, manifest) for r in responses]
    exact = sum(1 for t in trials if t.get("exact_match"))
    cats: dict[str, int] = {}
    for t in trials:
        for e in t.get("errors", []): cats[e] = cats.get(e, 0) + 1
    return {"trial_count": len(trials), "exact_match_count": exact, "pass_rate": exact / len(trials) if trials else 0.0, "failure_categories": cats, "trials": trials}

def analyze_progress(observations: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Validate the ordered progress/result/terminal lifecycle, returning stable errors."""
    errors: list[str] = []
    progress: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    last_seq = -1; last_elapsed = -1; last_processed = 0; last_generated = 0
    last_phase = -1; total: int | None = None; terminal_seen = False
    for item in observations:
        if not isinstance(item, dict):
            errors.append("malformed_observation"); continue
        kind = item.get("kind", "progress")
        seq, elapsed = item.get("sequence"), item.get("elapsed_ms")
        if not isinstance(seq, int) or isinstance(seq, bool): errors.append("malformed_sequence")
        elif seq <= last_seq: errors.append("decreasing_sequence")
        else: last_seq = seq
        if not isinstance(elapsed, int) or isinstance(elapsed, bool) or elapsed < 0: errors.append("malformed_elapsed")
        elif elapsed <= last_elapsed: errors.append("decreasing_elapsed")
        else: last_elapsed = elapsed
        if kind == "terminal":
            terminals.append(item); terminal_seen = True
            if item.get("state") not in {"completed", "cancelled", "failed"}: errors.append("invalid_terminal_state")
            if len(terminals) > 1: errors.append("duplicate_terminal")
            continue
        if kind == "result":
            results.append(item)
            if terminal_seen: errors.append("result_after_terminal")
            if item.get("status") != "success": errors.append("invalid_result")
            continue
        if kind != "progress": errors.append("invalid_observation_kind"); continue
        if terminal_seen: errors.append("progress_after_terminal")
        progress.append(item)
        phase = item.get("phase")
        if phase not in PHASES: errors.append("invalid_phase"); continue
        if PHASES[phase] < last_phase or (last_phase >= 0 and PHASES[phase] > last_phase + 1): errors.append("invalid_phase_transition")
        last_phase = max(last_phase, PHASES[phase])
        p, c, g, current_total = (item.get(key) for key in ("processed_prompt_tokens",
            "cached_prompt_tokens", "generated_tokens", "total_prompt_tokens"))
        if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (p, c, g, current_total)):
            errors.append("malformed_telemetry"); continue
        if current_total <= 0: errors.append("invalid_prompt_total")
        if total is None: total = current_total
        elif total != current_total: errors.append("changing_prompt_total")
        if p < last_processed: errors.append("decreasing_processed")
        if g < last_generated: errors.append("decreasing_generated")
        if c > p: errors.append("cached_exceeds_processed")
        if p > current_total: errors.append("processed_exceeds_total")
        last_processed, last_generated = p, g
    if not progress: errors.append("progress_missing")
    if len(terminals) != 1: errors.append("terminal_missing" if not terminals else "terminal_conflict")
    terminal_state = terminals[0].get("state") if len(terminals) == 1 else None
    if terminal_state == "cancelled" and results: errors.append("result_after_cancellation")
    if terminal_state == "completed":
        if len(results) != 1: errors.append("successful_result_missing")
        if total is None or last_processed != total: errors.append("incomplete_prefill")
        if last_phase != PHASES["generating"]: errors.append("terminal_lifecycle_without_generation")
    return {"pass": not errors, "errors": list(dict.fromkeys(errors)),
        "progress_event_count": len(progress), "first_progress": progress[0] if progress else None,
        "final_progress": progress[-1] if progress else None, "terminal_state": terminal_state,
        "terminal_observation": terminals[0] if len(terminals) == 1 else None,
        "result_observed": len(results) == 1}


def summarize_metrics(*, start_s: float, preparing_end_s: float, prefill_end_s: float,
        first_token_s: float, end_s: float, prompt_tokens: int, output_tokens: int,
        request_budget_s: float) -> dict[str, Any]:
    """Calculate all P8 timings, failing closed for missing or unordered evidence."""
    numeric = (start_s, preparing_end_s, prefill_end_s, first_token_s, end_s, request_budget_s)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in numeric):
        return {"pass": False, "code": "timing_non_finite"}
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (prompt_tokens, output_tokens)):
        return {"pass": False, "code": "token_count_invalid"}
    if request_budget_s <= 0 or not (start_s <= preparing_end_s <= prefill_end_s <= first_token_s <= end_s):
        return {"pass": False, "code": "timing_order_invalid"}
    total = end_s - start_s; prefill = prefill_end_s - preparing_end_s
    decode = end_s - first_token_s
    if total > request_budget_s: return {"pass": False, "code": "request_budget_exceeded"}
    return {"pass": True, "preparing_duration_s": preparing_end_s - start_s,
        "prefill_duration_s": prefill, "time_to_first_token_s": first_token_s - start_s,
        "decode_duration_s": decode, "total_duration_s": total,
        "prompt_tokens": prompt_tokens, "output_tokens": output_tokens,
        "prompt_tokens_per_s": prompt_tokens / prefill if prefill > 0 else None,
        "decode_tokens_per_s": output_tokens / decode if decode > 0 else None,
        "request_budget_s": request_budget_s, "completion_margin_s": request_budget_s - total}

def compare_kv_estimate(estimate: dict[str, Any], runtime: dict[str, Any], exact_required: bool = True, tolerance_bytes: int = 4096) -> dict[str, Any]:
    est = estimate.get("exact_kv_allocation_bytes") or estimate.get("exact_kv_cache_bytes")
    obs = runtime.get("kv_allocation_bytes")
    fallback = bool(estimate.get("fallback") or estimate.get("kv_cache_breakdown", {}).get("exact_allocation_available") is False)
    if exact_required and (fallback or not isinstance(est, int) or not isinstance(obs, int)):
        return {"pass": False, "code": "exact_kv_diagnostics_absent_or_fallback"}
    if not isinstance(est, int) or not isinstance(obs, int): return {"pass": False, "code": "kv_diagnostics_absent"}
    delta = abs(est-obs)
    return {"pass": delta <= tolerance_bytes, "estimated_bytes": est, "observed_bytes": obs, "delta_bytes": delta, "tolerance_bytes": tolerance_bytes, "alignment_rule": "exact GGML allocation bytes must match runtime diagnostics within one 4KiB page"}

def sanitize(value: Any) -> Any:
    if isinstance(value, dict): return {str(k)[:64]: sanitize(v) for k,v in value.items() if str(k).lower() not in SENSITIVE_KEYS}
    if isinstance(value, list): return [sanitize(v) for v in value[:100]]
    if isinstance(value, str):
        s = value[:512]
        for pat in SECRET_PATTERNS: s = pat.sub("<redacted>", s)
        return s
    return value

def validate_report(report: Any) -> None:
    """Validate the stable, privacy-safe v1 report envelope before replacement."""
    if not isinstance(report, dict): raise ValueError("report_schema_invalid")
    required = {"schema_version", "mode", "status", "fixture"}
    if not required.issubset(report): raise ValueError("report_schema_missing")
    if report["schema_version"] != SCHEMA_VERSION: raise ValueError("report_schema_version_invalid")
    if report["mode"] not in {"semantic-evaluation", "packaged-runtime"}: raise ValueError("report_mode_invalid")
    if report["status"] not in {"passed", "failed", "not_run"}: raise ValueError("report_status_invalid")
    fixture = report["fixture"]
    if not isinstance(fixture, dict) or not all(isinstance(fixture.get(key), str) and fixture[key]
            for key in ("id", "version", "scenario", "sha256")):
        raise ValueError("report_fixture_invalid")
    if fixture["version"] != FIXTURE_VERSION: raise ValueError("report_fixture_version_invalid")
    def finite(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    def reject_non_finite(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values(): reject_non_finite(child)
        elif isinstance(value, list):
            for child in value: reject_non_finite(child)
        elif isinstance(value, float) and not math.isfinite(value): raise ValueError("report_non_finite")
    reject_non_finite(report)
    if report["mode"] == "semantic-evaluation":
        semantic = report.get("semantic")
        if not isinstance(semantic, dict) or not isinstance(semantic.get("semantic_pass"), bool):
            raise ValueError("report_semantic_invalid")
        return
    if report["status"] != "passed" and "runtime" not in report:
        if not isinstance(report.get("code"), str) or not report["code"]: raise ValueError("report_failure_code_invalid")
        return
    runtime, backend, context = report.get("runtime"), report.get("backend"), report.get("context")
    if not isinstance(runtime, dict) or not all(isinstance(runtime.get(key), str) and runtime[key]
            for key in ("app_identity", "runtime_identity", "build_identity", "model_fingerprint")):
        raise ValueError("report_runtime_invalid")
    if not isinstance(backend, dict) or set(backend) != {"requested", "selected", "used"} or \
            any(value not in {"cpu", "metal", "cuda"} for value in backend.values()):
        raise ValueError("report_backend_invalid")
    if not isinstance(context, dict) or context.get("tier") not in {"8k-fast", "64k-full"} or not all(
            isinstance(context.get(key), int) and context[key] >= 0 for key in
            ("window_tokens", "output_reservation_tokens", "prompt_tokens", "output_tokens")):
        raise ValueError("report_context_invalid")
    progress, metrics, semantic = report.get("progress"), report.get("metrics"), report.get("semantic")
    if not isinstance(progress, dict) or progress.get("pass") is not True or not isinstance(progress.get("progress_event_count"), int):
        raise ValueError("report_progress_invalid")
    metric_keys = ("preparing_duration_s", "prefill_duration_s", "time_to_first_token_s",
        "decode_duration_s", "total_duration_s", "prompt_tokens", "output_tokens",
        "request_budget_s", "completion_margin_s")
    if not isinstance(metrics, dict) or metrics.get("pass") is not True or not all(finite(metrics.get(key)) for key in metric_keys):
        raise ValueError("report_metrics_invalid")
    for key in ("prompt_tokens_per_s", "decode_tokens_per_s"):
        if key not in metrics or (metrics[key] is not None and not finite(metrics[key])):
            raise ValueError("report_metrics_invalid")
    if not isinstance(semantic, dict) or not isinstance(semantic.get("semantic_pass"), bool):
        raise ValueError("report_semantic_invalid")
    aggregate = report.get("aggregate_semantic")
    if not isinstance(aggregate, dict) or not isinstance(aggregate.get("trial_count"), int) or \
            not isinstance(aggregate.get("exact_match_count"), int) or not finite(aggregate.get("pass_rate")):
        raise ValueError("report_semantic_aggregate_invalid")

def write_report_atomic(out_dir: Path, report: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = sanitize({"schema_version": SCHEMA_VERSION, **report})
    validate_report(report)
    text = _canonical_json(report) + "\n"
    fd, name = tempfile.mkstemp(prefix=".p8-report-", suffix=".json", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(text)
    dest = out_dir / "p8_benchmark_report.json"
    os.replace(name, dest)
    return dest

def platform_memory_probe(command: list[str], timeout_s: float = 2.0) -> dict[str, Any]:
    try:
        cp = subprocess.run(command, capture_output=True, text=True, timeout=timeout_s, check=False)
    except FileNotFoundError: return {"available": False, "code": "probe_absent"}
    except subprocess.TimeoutExpired: return {"available": False, "code": "probe_timeout"}
    stdout = cp.stdout[:1048576]
    try: payload = json.loads(stdout)
    except Exception: return {"available": False, "code": "probe_malformed", "stdout_tail": sanitize(stdout[-200:])}
    return sanitize({"available": cp.returncode == 0, "code": "ok" if cp.returncode == 0 else "probe_failed", "payload": payload})

def _valid_relay_url(value: str) -> bool:
    parsed = urlparse(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.fragment):
        return False
    try:
        parsed.port
    except ValueError:
        return False
    loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    return parsed.scheme == "https" or loopback


def p8_operator_mode(backend: str) -> str:
    """Map an attested backend to the operator control value."""
    if backend == "cpu":
        return "cpu"
    if backend in {"metal", "cuda"}:
        return "gpu"
    raise ValueError("unsupported P8 backend")


def apply_p8_context_tier(driver: object, context_tier: str) -> str:
    """Set and read back the landing-page tier through the browser boundary."""
    return driver.execute_script(
        "const v=document.querySelector('#app').__vue__; v.selectedContextTier=arguments[0]; "
        "v.persistContextTier(arguments[0]); return v.selectedContextTier;", context_tier)


def classify_p8_landing_state(state: object) -> tuple[str, str | None]:
    """Classify the landing-page API response lifecycle without trusting UI prose."""
    if not isinstance(state, dict):
        return "failed", None
    history = state.get("h")
    if not isinstance(history, list):
        return "failed", None
    assistants = [entry for entry in history
        if isinstance(entry, dict) and entry.get("role") == "assistant"]
    if state.get("b") is True or not assistants:
        return "running", None
    assistant = assistants[-1]
    response = assistant.get("content")
    if (assistant.get("isTyping") is False and "finishReason" in assistant
            and isinstance(response, str)):
        return "completed", response
    return "failed", None


def observe_post_terminal(poller: Callable[[], object], *, clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep, window_s: float = 0.1,
        interval_s: float = 0.01) -> list[object]:
    """Collect a short bounded post-terminal window without a fixed long sleep."""
    if not math.isfinite(window_s) or window_s < 0: raise ValueError("post_terminal_window_invalid")
    deadline = clock() + window_s; observed: list[object] = []
    while clock() < deadline:
        observed.append(poller())
        sleeper(min(interval_s, max(0.0, deadline - clock())))
    return observed


def _run_owned_runner(command: list[str], timeout_s: float,
        cleanup_timeout_s: float, *, popen: Callable[..., Any] = subprocess.Popen,
        cleanup_run: Callable[..., Any] = subprocess.run,
        killpg: Callable[[int, int], Any] = os.killpg,
        platform_name: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run one owned process group without buffering output or killing by name."""
    kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    owned_platform = os.name if platform_name is None else platform_name
    if owned_platform == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        kwargs["start_new_session"] = True
    process = popen(command, **kwargs)  # noqa: S603
    chunks: deque[bytes] = deque(maxlen=8)
    def drain_output() -> None:
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read(256), b""):
            chunks.append(chunk)
    drain = threading.Thread(target=drain_output, daemon=True)
    drain.start()
    try:
        returncode = process.wait(timeout=timeout_s + cleanup_timeout_s)
    except subprocess.TimeoutExpired:
        if owned_platform == "nt":
            try:
                cleanup_run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=cleanup_timeout_s, check=False)  # noqa: S603
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            try:
                process.wait(timeout=cleanup_timeout_s)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=cleanup_timeout_s)
        else:
            with contextlib.suppress(ProcessLookupError):
                killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=cleanup_timeout_s)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=cleanup_timeout_s)
        raise
    drain.join(timeout=cleanup_timeout_s)
    tail = b"".join(chunks)[-2048:].decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(command, returncode, stdout=tail)


def invoke_packaged_runtime_adapter(*, fixture_id: str = "small-8k", scenario: str = "structured-extraction", timeout_s: float = 30.0,
        model: str | None = None, backend: str | None = None, relay_url: str | None = None,
        cleanup_timeout_s: float | None = None, app_binary: str | None = None,
        context_tier: str = "64k-full", report_only: bool = False,
        external_prompt: str | None = None, external_manifest: dict[str, Any] | None = None,
        subprocess_run: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Run the repository-owned packaged desktop E2E runner and validate its evidence."""
    missing = [name for name, value in {"app_binary": app_binary, "model": model, "backend": backend,
        "relay_url": relay_url, "timeout_s": timeout_s, "cleanup_timeout_s": cleanup_timeout_s}.items()
        if value in (None, "")]
    if missing:
        return {"pass": False, "code": "packaged_prerequisites_missing", "missing": missing}
    model_path = Path(str(model))
    if not model_path.is_file() or not os.access(model_path, os.R_OK):
        return {"pass": False, "code": "model_artifact_invalid"}
    app_path = Path(str(app_binary))
    if not app_path.is_file() or not os.access(app_path, os.X_OK):
        return {"pass": False, "code": "packaged_app_invalid"}
    if backend not in {"metal", "cuda", "cpu"}:
        return {"pass": False, "code": "backend_unsupported"}
    if not _valid_relay_url(str(relay_url)):
        return {"pass": False, "code": "relay_url_invalid"}
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in (timeout_s, cleanup_timeout_s)):
        return {"pass": False, "code": "timeout_invalid"}
    if (external_prompt is None) != (external_manifest is None):
        return {"pass": False, "code": "external_fixture_pair_required"}
    if external_prompt is None:
        prompt, manifest = generate_fixture(fixture_id, scenario=scenario)
    else:
        prompt, manifest = external_prompt, external_manifest
    try:
        validate_manifest(manifest, prompt)
    except ValueError as exc:
        return {"pass": False, "code": str(exc)}
    if manifest["fixture_id"] != fixture_id:
        return {"pass": False, "code": "manifest_fixture_mismatch"}
    if manifest["scenario"] != scenario:
        return {"pass": False, "code": "manifest_scenario_mismatch"}
    profile = get_context_profile(context_tier)
    prompt_budget = profile.total_context_tokens - profile.default_output_reservation_tokens
    if manifest["actual_tokens"] > prompt_budget:
        return {"pass": False, "runtime_contract_pass": False,
            "code": "fixture_context_incompatible", "fixture_tokens": manifest["actual_tokens"],
            "prompt_budget_tokens": prompt_budget, "context_tier": context_tier}
    request = {"fixture_id": fixture_id, "prompt": prompt, "manifest": manifest,
        "model": str(model_path), "backend": backend, "relay_url": relay_url,
        "context_tier": context_tier, "request_timeout_s": timeout_s,
        "cleanup_timeout_s": cleanup_timeout_s}
    request_name = evidence_name = diagnostic_name = None
    try:
        request_fd, request_name = tempfile.mkstemp(prefix="p8-request-", suffix=".json")
        evidence_fd, evidence_name = tempfile.mkstemp(prefix="p8-evidence-", suffix=".json")
        if hasattr(os, "fchmod"):
            os.fchmod(request_fd, 0o600); os.fchmod(evidence_fd, 0o600)
        with os.fdopen(request_fd, "w", encoding="utf-8") as handle:
            json.dump(request, handle)
        os.close(evidence_fd)
        command = [sys.executable, str(Path(__file__).parents[2] / "desktop-tauri" / "scripts" /
            "test_desktop_operator_ui_e2e.py"), "--p8-request", request_name,
            "--p8-evidence", evidence_name, "--app-binary", str(app_path)]
        diagnostic_fd, diagnostic_name = tempfile.mkstemp(prefix="p8-runner-", suffix=".log")
        try:
            with os.fdopen(diagnostic_fd, "w+", encoding="utf-8") as diagnostic_handle:
                if subprocess_run is None:
                    completed = _run_owned_runner(command, timeout_s, cleanup_timeout_s)
                else:
                    completed = subprocess_run(command, stdout=diagnostic_handle,
                        stderr=subprocess.STDOUT, text=True,
                        timeout=timeout_s + cleanup_timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return {"pass": False, "code": "packaged_runner_timeout"}
        if completed.returncode != 0:
            tail = (completed.stdout or Path(diagnostic_name).read_text(
                encoding="utf-8", errors="replace"))[-2048:]
            return {"pass": False, "code": "packaged_runner_failed", "diagnostic_tail": sanitize(tail)}
        try:
            payload = json.loads(Path(evidence_name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"pass": False, "code": "packaged_evidence_malformed"}
    finally:
        for name in (request_name, evidence_name, diagnostic_name):
            if name:
                Path(name).unlink(missing_ok=True)
    if not isinstance(payload, dict):
        return {"pass": False, "code": "packaged_evidence_malformed"}
    required = {"app_identity", "runtime_identity", "bundled_runtime_identity", "build_identity",
        "backend_requested", "backend_selected", "backend_used", "model_fingerprint",
        "authoritative_prompt_tokens", "progress_events",
        "authoritative_tokenizer_evidence", "terminal_observation", "result_observation",
        "response_text", "start_s", "preparing_end_s", "prefill_end_s", "first_token_s", "end_s",
        "output_tokens", "post_terminal_observations"}
    missing_evidence = sorted(key for key in required if key not in payload or payload.get(key) in (None, "", {}))
    if missing_evidence:
        if "authoritative_tokenizer_evidence" in missing_evidence:
            return {"pass": False, "code": "authoritative_target_depth_unavailable",
                "missing_seam": "packaged_admission_render_and_tokenize_chat_prefix_counts"}
        return {"pass": False, "code": "packaged_evidence_missing", "missing": missing_evidence}
    if (not isinstance(payload["authoritative_prompt_tokens"], int) or
            isinstance(payload["authoritative_prompt_tokens"], bool) or
            payload["authoritative_prompt_tokens"] <= 0 or
            not isinstance(payload["output_tokens"], int) or isinstance(payload["output_tokens"], bool) or
            payload["output_tokens"] < 0 or not isinstance(payload["progress_events"], list) or
            not isinstance(payload["post_terminal_observations"], list)):
        return {"pass": False, "code": "packaged_evidence_malformed"}
    authoritative_offsets, depth_error = _validate_authoritative_tokenizer_evidence(
        payload["authoritative_tokenizer_evidence"], manifest,
        payload["runtime_identity"], payload["authoritative_prompt_tokens"])
    if depth_error:
        return {"pass": False, "code": depth_error}
    observations = [*payload.get("progress_events", []), payload["result_observation"],
        payload["terminal_observation"], *payload["post_terminal_observations"]]
    progress = analyze_progress(observations)
    semantic = evaluate_semantic(payload.get("response_text", ""), manifest)
    metrics = summarize_metrics(start_s=payload["start_s"], preparing_end_s=payload["preparing_end_s"],
        prefill_end_s=payload["prefill_end_s"], first_token_s=payload["first_token_s"],
        end_s=payload["end_s"], prompt_tokens=payload["authoritative_prompt_tokens"],
        output_tokens=payload["output_tokens"], request_budget_s=timeout_s)
    evidence = {
        "runner_kind": "repository_packaged_desktop_webdriver",
        "fixture": {"id": fixture_id, "sha256": manifest.get("fixture_sha256"),
            "estimated_prompt_tokens": manifest.get("actual_tokens"),
            "estimated_tokenizer": manifest.get("token_count_provenance"),
            "authoritative_prompt_tokens": payload.get("authoritative_prompt_tokens"),
            "authoritative_target_offsets_tokens": authoritative_offsets,
            "authoritative_target_ratios": {key: value / payload["authoritative_prompt_tokens"]
                for key, value in authoritative_offsets.items()}},
        "semantic": semantic,
        "progress": progress,
        "metrics": metrics,
        "memory": payload.get("memory", {}),
        "runtime": {key: payload[key] for key in ("app_identity", "runtime_identity",
            "bundled_runtime_identity", "build_identity", "backend_requested", "backend_selected",
            "backend_used", "model_fingerprint",
            "authoritative_prompt_tokens")},
    }
    if "kv_estimate" in payload or "kv_runtime" in payload:
        evidence["kv_compare"] = compare_kv_estimate(payload.get("kv_estimate", {}), payload.get("kv_runtime", {}))
    runtime = evidence["runtime"]
    identity_ok = (runtime["app_identity"] not in {"dev", "mock", "unknown"} and
        runtime["runtime_identity"] == runtime.get("bundled_runtime_identity") and
        runtime["backend_requested"] == backend and runtime["backend_selected"] == backend and
        runtime["backend_used"] == backend)
    final_total = (progress.get("final_progress") or {}).get("total_prompt_tokens")
    runtime_contract_pass = bool(progress.get("pass") and metrics.get("pass") and identity_ok
        and payload["authoritative_prompt_tokens"] == final_total)
    if "kv_compare" in evidence:
        runtime_contract_pass = runtime_contract_pass and bool(evidence["kv_compare"].get("pass"))
    passed = bool(runtime_contract_pass and semantic.get("semantic_pass"))
    evidence["runtime_contract_pass"] = runtime_contract_pass
    evidence["report_only_accepted"] = bool(report_only and runtime_contract_pass and not semantic.get("semantic_pass"))
    evidence["pass"] = passed
    evidence["code"] = "ok" if passed else "packaged_contract_failed"
    return sanitize(evidence)

def cancellation_recovery_result(events: list[dict[str, Any]], *, phase: str, threshold: int, followup_ok: bool, cleanup_s: float, cleanup_budget_s: float = 30.0, late_result: bool = False, stale_progress: bool = False) -> dict[str, Any]:
    """Evaluate canned progress-triggered cancellation and clean-worker recovery."""
    ack = False
    for ev in events:
        if ev.get("phase") == phase:
            count = ev.get("processed_prompt_tokens") if phase == "prefill" else ev.get("generated_tokens")
            if isinstance(count, int) and count >= threshold:
                ack = True
                break
    if events:
        last = events[-1]
        events = [*events, {"kind": "terminal", "state": "cancelled",
            "sequence": last.get("sequence", 0) + 1, "elapsed_ms": last.get("elapsed_ms", 0) + 1}]
    progress = analyze_progress(events)
    errors = []
    if not ack: errors.append("cancel_not_triggered")
    if not progress["pass"]: errors.extend(progress["errors"])
    if cleanup_s > cleanup_budget_s: errors.append("cleanup_timeout")
    if late_result: errors.append("late_result_after_cancel")
    if stale_progress: errors.append("stale_progress_after_cancel")
    if not followup_ok: errors.append("followup_worker_failed")
    return {"pass": not errors, "errors": errors, "trigger_phase": phase, "trigger_threshold": threshold, "cleanup_s": cleanup_s, "followup_ok": followup_ok}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="P8 packaged-runtime benchmark harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate-fixture"); g.add_argument("--fixture", choices=FIXTURES, required=True); g.add_argument("--scenario", choices=("single-needle", "structured-extraction"), required=True); g.add_argument("--out-dir", required=True); g.add_argument("--seed", default=DEFAULT_SEED)
    e = sub.add_parser("evaluate"); e.add_argument("--manifest", required=True); e.add_argument("--response", required=True); e.add_argument("--strict", action="store_true"); e.add_argument("--out-dir", required=True)
    r = sub.add_parser("packaged-runtime", help="run the installed desktop through repository WebDriver control")
    r.add_argument("--out-dir", required=True); r.add_argument("--fixture", choices=FIXTURES, default="small-8k")
    r.add_argument("--scenario", choices=("single-needle", "structured-extraction"), default="structured-extraction")
    r.add_argument("--app-binary", required=True); r.add_argument("--model", required=True)
    r.add_argument("--backend", choices=("metal", "cuda", "cpu"), required=True); r.add_argument("--relay-url", required=True)
    r.add_argument("--context-tier", choices=("8k-fast", "64k-full"), default="64k-full")
    r.add_argument("--prompt", help="external UTF-8 prompt (requires --manifest)")
    r.add_argument("--manifest", help="external validated manifest (requires --prompt)")
    r.add_argument("--request-timeout", type=float, default=600.0); r.add_argument("--cleanup-timeout", type=float, default=30.0)
    r.add_argument("--report-only", action="store_true", help="preserve semantic failures; runtime failures remain nonzero")
    args = p.parse_args(argv)
    if args.cmd == "generate-fixture":
        prompt, manifest = generate_fixture(args.fixture, args.seed, scenario=args.scenario); validate_manifest(manifest, prompt); out=Path(args.out_dir); out.mkdir(parents=True, exist_ok=True); (out/f"{args.fixture}.prompt.txt").write_text(prompt); (out/f"{args.fixture}.manifest.json").write_text(_canonical_json(manifest)+"\n"); print(f"generated {args.fixture}: scenario={args.scenario} requested={manifest['requested_tokens']} actual={manifest['actual_tokens']} sha256={manifest['fixture_sha256']}"); return 0
    if args.cmd == "evaluate":
        manifest=json.loads(Path(args.manifest).read_text()); validate_manifest(manifest); response=Path(args.response).read_text(); score=evaluate_semantic(response, manifest); path=write_report_atomic(Path(args.out_dir), {"mode":"semantic-evaluation", "status":"passed" if score["semantic_pass"] else "failed", "semantic":score,"fixture":{"id":manifest["fixture_id"], "version":manifest["fixture_version"], "scenario":manifest["scenario"], "sha256":manifest["fixture_sha256"]}}); print(f"semantic_pass={score.get('semantic_pass', False)} report={path}"); return 1 if args.strict and not score.get("semantic_pass") else 0
    if args.cmd == "packaged-runtime":
        if bool(args.prompt) != bool(args.manifest):
            p.error("--prompt and --manifest are mutually required")
        external_prompt = _read_bounded_text(args.prompt) if args.prompt else None
        external_manifest = json.loads(_read_bounded_text(args.manifest, 1024 * 1024)) if args.manifest else None
        evidence = invoke_packaged_runtime_adapter(fixture_id=args.fixture, scenario=args.scenario, timeout_s=args.request_timeout,
            app_binary=args.app_binary, model=args.model, backend=args.backend, relay_url=args.relay_url,
            cleanup_timeout_s=args.cleanup_timeout, context_tier=args.context_tier,
            report_only=args.report_only, external_prompt=external_prompt,
            external_manifest=external_manifest)
        if evidence.get("runtime_contract_pass"):
            profile = get_context_profile(args.context_tier)
            report = {"mode":"packaged-runtime", "status":"passed" if evidence.get("pass") else "failed",
                "code":"ok" if evidence.get("pass") else "semantic_failure", "overall_pass":bool(evidence.get("pass")),
                "report_only_accepted":bool(evidence.get("report_only_accepted")), "fixture":{
                "id":args.fixture, "version":FIXTURE_VERSION, "scenario":args.scenario,
                "sha256":evidence["fixture"]["sha256"]}, "runtime":evidence["runtime"],
                "backend":{"requested":evidence["runtime"]["backend_requested"],
                    "selected":evidence["runtime"]["backend_selected"], "used":evidence["runtime"]["backend_used"]},
                "context":{"tier":args.context_tier, "window_tokens":profile.total_context_tokens,
                    "output_reservation_tokens":profile.default_output_reservation_tokens,
                    "prompt_tokens":evidence["fixture"]["authoritative_prompt_tokens"],
                    "output_tokens":evidence["metrics"]["output_tokens"]},
                "progress":evidence["progress"], "metrics":evidence["metrics"],
                "semantic":evidence["semantic"], "aggregate_semantic":{"trial_count":1,
                    "exact_match_count":int(evidence["semantic"]["semantic_pass"]),
                    "pass_rate":float(evidence["semantic"]["semantic_pass"])}}
        else:
            report = {"mode":"packaged-runtime", "status":"not_run", "code":evidence.get("code", "packaged_contract_failed"),
                "fixture":{"id":args.fixture, "version":FIXTURE_VERSION, "scenario":args.scenario,
                    "sha256":"unavailable"}}
        path=write_report_atomic(Path(args.out_dir), report)
        print(f"packaged_runtime_pass={evidence.get('pass', False)} report={path}")
        return 0 if evidence.get("pass") or evidence.get("report_only_accepted") else 1
    return 2
if __name__ == "__main__": raise SystemExit(main())
