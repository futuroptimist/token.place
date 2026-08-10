"""Long-context packaged-runtime benchmark harness utilities.

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

import psutil

from utils.context_profiles import get_context_profile

SCHEMA_VERSION = "long-context-benchmark-report-v2"
FIXTURE_VERSION = "long-context-semantic-haystack-v6"
DEFAULT_SEED = "long-context-benchmark-default"
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

# The watchdog contract is deliberately additive: setup and evidence work may
# not borrow from the inference request's independently measured allowance.
PACKAGED_SETUP_BUDGET_S = 300.0
PACKAGED_FINALIZATION_BUDGET_S = 120.0
PACKAGED_PHASE_STATUS_VERSION = "packaged-runner-phase-v2"
PACKAGED_PHASES = (
    "runner_startup", "webdriver_ready", "desktop_ready", "operator_ready",
    "landing_page_ready", "request_active", "response_received",
    "cancellation_validation", "evidence_finalization", "cleanup",
)
PACKAGED_FAILURE_REASONS = frozenset({
    "cleanup_failure",
    "vue_not_ready", "client_keypair_not_ready", "model_selection_not_ready",
    "requested_context_tier_not_applied", "message_input_not_populated",
    "send_button_not_enabled", "packaged_runner_failure",
    "authoritative_local_progress_missing", "local_prefill_phase_missing",
    "local_generating_phase_missing", "positive_generated_token_progress_missing",
    "local_timing_record_malformed", "response_usage_missing_or_inconsistent",
    "local_telemetry_configuration_mismatch",
    "encrypted_progress_delivery_invalid",
})

_LOCAL_PROGRESS_RE = re.compile(
    r"api_v1\.local_progress request_id=(\S+) worker_generation=(\d+) sequence=(\d+) "
    r"phase=(\S+) total_prompt_tokens=(\d+) cached_prompt_tokens=(\d+) "
    r"processed_prompt_tokens=(\d+) generated_tokens=(\d+) elapsed_ms=(\d+)\s*$")
_INFERENCE_COMPLETE_RE = re.compile(
    r"api_v1\.inference_complete active_tier=(\S+) prompt_tokens=(\d+) "
    r"output_reservation=(\d+) admission_result=admitted "
    r"inference_duration_seconds=([0-9]+(?:\.[0-9]+)?) safe_error_code=none\s*$")


def parse_packaged_local_telemetry(text: str) -> dict[str, Any]:
    """Extract only allowlisted P3 records from a post-boundary driver-log slice."""
    progress: list[dict[str, Any]] = []
    correlations: set[tuple[str, int]] = set()
    completions: list[dict[str, Any]] = []
    malformed = False
    for raw_line in text.splitlines():
        if "api_v1.local_progress" in raw_line:
            match = _LOCAL_PROGRESS_RE.search(raw_line)
            if not match:
                malformed = True
                continue
            request_id, generation, sequence, phase, total, cached, processed, generated, elapsed = match.groups()
            correlations.add((request_id, int(generation)))
            progress.append({"sequence": int(sequence), "phase": phase,
                "total_prompt_tokens": int(total), "cached_prompt_tokens": int(cached),
                "processed_prompt_tokens": int(processed), "generated_tokens": int(generated),
                "elapsed_ms": int(elapsed)})
        elif "api_v1.inference_complete" in raw_line:
            match = _INFERENCE_COMPLETE_RE.search(raw_line)
            if not match:
                malformed = True
                continue
            tier, prompt, reservation, duration = match.groups()
            completions.append({"active_tier": tier, "prompt_tokens": int(prompt),
                "output_reservation": int(reservation),
                "inference_duration_seconds": float(duration)})
    # Correlation values are deliberately discarded rather than becoming evidence.
    return {"progress_events": progress, "inference_complete": completions,
        "ambiguous": len(correlations) != 1 or len(completions) != 1,
        "malformed": malformed}


def validate_authoritative_local_telemetry(value: Any, *, completed: bool = True,
        expected_tier: str | None = None,
        expected_output_reservation: int | None = None) -> dict[str, Any]:
    """Validate sanitized P3 progress and completion timing without inventing observations."""
    if not isinstance(value, dict) or value.get("malformed"):
        raise ValueError("local_timing_record_malformed")
    events, completions = value.get("progress_events"), value.get("inference_complete")
    if not isinstance(events, list) or not events:
        raise ValueError("authoritative_local_progress_missing")
    if value.get("ambiguous"):
        raise ValueError("local_timing_record_malformed")
    if not isinstance(completions, list) or len(completions) != 1:
        raise ValueError("local_timing_record_malformed")
    last_sequence = last_elapsed = -1
    last_phase = -1
    last_processed = last_cached = last_generated = 0
    total: int | None = None
    phases: list[str] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != {"sequence", "phase", "total_prompt_tokens",
                "cached_prompt_tokens", "processed_prompt_tokens", "generated_tokens", "elapsed_ms"}:
            raise ValueError("local_timing_record_malformed")
        sequence, elapsed = event["sequence"], event["elapsed_ms"]
        counters = [event[key] for key in ("total_prompt_tokens", "cached_prompt_tokens",
            "processed_prompt_tokens", "generated_tokens")]
        if any(not isinstance(item, int) or isinstance(item, bool) or item < 0
                for item in [sequence, elapsed, *counters]):
            raise ValueError("local_timing_record_malformed")
        phase = event["phase"]
        if phase not in PHASES or sequence <= last_sequence or elapsed < last_elapsed:
            raise ValueError("local_timing_record_malformed")
        if last_phase >= 0 and (PHASES[phase] < last_phase or PHASES[phase] > last_phase + 1):
            raise ValueError("local_timing_record_malformed")
        current_total, cached, processed, generated = counters
        if current_total == 0:
            if not (index == 0 and phase == "preparing" and processed == cached == 0):
                raise ValueError("local_timing_record_malformed")
        elif total is None:
            total = current_total
        elif current_total != total:
            raise ValueError("local_timing_record_malformed")
        if (cached > processed or (current_total and processed > current_total)
                or processed < last_processed or cached < last_cached or generated < last_generated):
            raise ValueError("local_timing_record_malformed")
        last_sequence, last_elapsed, last_phase = sequence, elapsed, PHASES[phase]
        last_processed, last_cached, last_generated = processed, cached, generated
        phases.append(phase)
    if "prefill" not in phases:
        raise ValueError("local_prefill_phase_missing")
    if "generating" not in phases:
        raise ValueError("local_generating_phase_missing")
    if not any(e["phase"] == "generating" and e["generated_tokens"] > 0 for e in events):
        raise ValueError("positive_generated_token_progress_missing")
    completion = completions[0]
    if (set(completion) != {"active_tier", "prompt_tokens", "output_reservation",
            "inference_duration_seconds"} or not isinstance(completion["active_tier"], str)
            or any(not isinstance(completion[key], int) or isinstance(completion[key], bool)
                or completion[key] < 0 for key in ("prompt_tokens", "output_reservation"))
            or not isinstance(completion["inference_duration_seconds"], (int, float))
            or isinstance(completion["inference_duration_seconds"], bool)
            or not math.isfinite(completion["inference_duration_seconds"])
            or completion["inference_duration_seconds"] < 0):
        raise ValueError("local_timing_record_malformed")
    if total is None or completion["prompt_tokens"] != total or (completed and last_processed != total):
        raise ValueError("local_timing_record_malformed")
    if ((expected_tier is not None and completion["active_tier"] != expected_tier)
            or (expected_output_reservation is not None
                and completion["output_reservation"] != expected_output_reservation)):
        raise ValueError("local_telemetry_configuration_mismatch")
    first_prefill = next(e for e in events if e["phase"] == "prefill")
    first_generating = next(e for e in events if e["phase"] == "generating")
    first_token = next(e for e in events if e["phase"] == "generating" and e["generated_tokens"] > 0)
    return {"events": events, "prompt_tokens": total, "generated_progress_tokens": last_generated,
        "phases": list(dict.fromkeys(phases)), "preparing_end_s": first_prefill["elapsed_ms"] / 1000,
        "prefill_end_s": first_generating["elapsed_ms"] / 1000,
        "first_token_s": first_token["elapsed_ms"] / 1000,
        "inference_duration_s": completion["inference_duration_seconds"]}


def validate_encrypted_progress_delivery(events: Any, authoritative: dict[str, Any]) -> dict[str, Any]:
    """Validate P6 delivery as a compatible, explicitly best-effort projection."""
    if not isinstance(events, list) or not events:
        raise ValueError("encrypted_progress_delivery_invalid")
    local_events = authoritative["events"]
    local_index = 0
    comparison_keys = set(local_events[0]) - {"sequence"}
    last_sequence = 0
    for event in events:
        if (not isinstance(event, dict)
                or set(event) != {*comparison_keys, "sequence", "schema_version"}
                or not isinstance(event["schema_version"], int)
                or isinstance(event["schema_version"], bool)
                or event["schema_version"] != 1
                or not isinstance(event["sequence"], int)
                or isinstance(event["sequence"], bool)
                or event["sequence"] <= last_sequence):
            raise ValueError("encrypted_progress_delivery_invalid")
        # Browser polling can miss replaceable best-effort updates, so observed
        # delivery sequences need only remain positive and strictly increasing.
        # Match their values as an ordered projection of local events.
        while (local_index < len(local_events)
                and any(event[key] != local_events[local_index][key]
                    for key in comparison_keys)):
            local_index += 1
        if local_index == len(local_events):
            raise ValueError("encrypted_progress_delivery_invalid")
        local_index += 1
        last_sequence = event["sequence"]
    phases = list(dict.fromkeys(event["phase"] for event in events))
    return {"pass": True, "best_effort": True, "progress_event_count": len(events),
        "observed_phases": phases, "terminal_overtook_generating_update":
            "generating" not in phases and "generating" in authoritative["phases"]}


def packaged_cancellation_budget_s(request_timeout_s: float, observation_window_s: float,
        recovery_timeout_s: float) -> float:
    """Return the additive upper bound for the opt-in cancellation sequence."""
    # Two trigger waits, two quiescence windows, two asynchronous cancellation
    # acknowledgements, two scenario follow-ups, operator stop, restart
    # stability, relay registration, and the post-restart follow-up.
    return 2 * request_timeout_s + 2 * observation_window_s + 8 * recovery_timeout_s


def packaged_phase_remaining(deadline: float, timeout_message: str, *,
        clock: Callable[[], float] = time.monotonic, cap: float | None = None) -> float:
    """Return one phase's remaining allowance, failing closed at its deadline."""
    remaining = deadline - clock()
    if remaining <= 0:
        raise RuntimeError(timeout_message)
    return min(remaining, cap) if cap is not None else remaining


def start_phase_after(work: Callable[[], Any], allowance_s: float, *,
        clock: Callable[[], float] = time.monotonic) -> tuple[Any, float]:
    """Run the preceding phase before starting a complete independent allowance."""
    result = work()
    return result, clock() + allowance_s


class PackagedRunnerTimeout(subprocess.TimeoutExpired):
    """A watchdog expiry carrying only bounded owned-cleanup outcome."""

    def __init__(self, command: list[str], timeout: float, cleanup_succeeded: bool):
        super().__init__(command, timeout)
        self.cleanup_succeeded = cleanup_succeeded

@dataclass(frozen=True)
class FixtureSpec:
    fixture_id: str
    requested_tokens: int
    approximate: bool = False

FIXTURES = {
    "small-8k": FixtureSpec("small-8k", 8192 - 1024),
    "intermediate-32k": FixtureSpec("intermediate-32k", 32768),
    "long-55k": FixtureSpec("long-55k", 55254, True),
}
STRUCTURED_HEADINGS = {
    "VII": "VII. They were obliged to camp out",
    "XIV": "XIV. The Winged Monkeys",
    "XXI": "XXI. The Lion Becomes the King",
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
    if evidence.get("fixture_sha256") != manifest.get("fixture_sha256"):
        return None, "authoritative_target_depth_stale"
    offsets = evidence.get("target_offsets_tokens")
    if (not isinstance(offsets, dict) or set(offsets) != set(manifest["targets"])
            or not all(isinstance(value, int) and not isinstance(value, bool)
                and 0 < value < total_prompt_tokens for value in offsets.values())):
        return None, "authoritative_target_depth_malformed"
    if len(set(offsets.values())) != len(offsets):
        return None, "authoritative_target_depth_ambiguous"
    expected_order = sorted(manifest["targets"],
        key=lambda key: manifest["targets"][key]["requested_ratio"])
    if sorted(offsets, key=offsets.get) != expected_order:
        return None, "authoritative_target_depth_ordering"
    if any(abs(offsets[key] / total_prompt_tokens
            - manifest["targets"][key]["requested_ratio"]) > 0.03 for key in offsets):
        return None, "authoritative_target_depth_ratio"
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
    toc = "\n".join(["Table of Contents", *STRUCTURED_HEADINGS.values()])
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
    target_prefix_utf8_bytes: dict[str, int] = {}
    filler_i = 0
    while True:
        joined = "\n".join(prompt_parts)
        cur = _count_tokens(joined, tokenizer)
        if cur >= spec.requested_tokens:
            break
        ratio = cur / max(spec.requested_tokens, 1)
        inserted = False
        for chap, pos in positions.items():
            if chap not in target_markers and ratio >= pos:
                if chap in chapter_sentences:
                    addition = f"\nChapter {chap}: {STRUCTURED_HEADINGS[chap]}\n{chapter_sentences[chap]}"
                elif chap == "needle":
                    addition = f"NEEDLE FACT: {targets['needle']}"
                else:
                    addition = f"RECORD CANARY: {canary}"
                if chap in chapter_sentences:
                    prefix = "\n".join(prompt_parts) + "\n" + addition[:addition.rfind("\n") + 1]
                elif chap == "needle":
                    prefix = "\n".join(prompt_parts) + "\nNEEDLE FACT: "
                else:
                    prefix = "\n".join(prompt_parts) + "\nRECORD CANARY: "
                candidate = "\n".join([*prompt_parts, addition])
                if _count_tokens(candidate, tokenizer) > spec.requested_tokens:
                    break
                prompt_parts.append(addition)
                target_markers[chap] = _count_tokens(prefix, tokenizer)
                target_prefix_utf8_bytes[chap] = len(prefix.encode("utf-8"))
                inserted = True
        if not inserted:
            decoy = hashlib.sha256(f"{seed}:{fixture_id}:{filler_i}".encode()).hexdigest()[:16]
            addition = f"Decoy paragraph {filler_i:05d} repeats chapter-title-like text but contains no answer. Similar marker needle-{decoy} is not the requested fact."
            if _count_tokens("\n".join([*prompt_parts, addition]), tokenizer) > spec.requested_tokens:
                break
            prompt_parts.append(addition)
            filler_i += 1
    for chap in (() if scenario == "single-needle" else ("VII", "XIV", "XXI")):
        if chap not in target_markers:
            addition = f"\nChapter {chap}: {STRUCTURED_HEADINGS[chap]}\n{chapter_sentences[chap]}"
            prefix = "\n".join(prompt_parts) + "\n" + addition[:addition.rfind("\n") + 1]
            target_markers[chap] = _count_tokens(prefix, tokenizer)
            target_prefix_utf8_bytes[chap] = len(prefix.encode("utf-8"))
            prompt_parts.append(addition)
    prompt = "\n".join(prompt_parts).rstrip() + "\n"
    actual = _count_tokens(prompt, tokenizer)
    manifest = {
        "fixture_version": FIXTURE_VERSION, "fixture_id": fixture_id, "seed": seed,
        "scenario": scenario,
        "requested_tokens": spec.requested_tokens, "actual_tokens": actual, "tokenizer": "supplied-callback" if tokenizer else "whitespace-ci",
        "token_count_provenance": {"kind": "estimate", "tokenizer_id": "supplied-callback" if tokenizer else "whitespace-ci", "authoritative": False, "units": "tokens"},
        "fixture_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "target_depths_tokens": target_markers,
        "target_prefix_utf8_bytes": target_prefix_utf8_bytes,
        "targets": {name: {"value": targets[name], "requested_offset_tokens": round(spec.requested_tokens * positions[name]),
            "requested_ratio": positions[name], "actual_offset_tokens": offset,
            "actual_ratio": offset / actual,
            "target_prefix_utf8_bytes": target_prefix_utf8_bytes[name]}
            for name, offset in target_markers.items()},
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
        "token_count_provenance", "target_depths_tokens", "target_prefix_utf8_bytes",
        "targets", "semantic_oracle"}
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
                or not isinstance(metadata.get("target_prefix_utf8_bytes"), int)
                or not isinstance(metadata.get("requested_ratio"), (int, float))
                or not isinstance(metadata.get("actual_ratio"), (int, float))
                or not 0 <= metadata["actual_offset_tokens"] < manifest["actual_tokens"]
                or metadata["target_prefix_utf8_bytes"] <= 0
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
    if manifest.get("target_prefix_utf8_bytes") != {
            name: metadata["target_prefix_utf8_bytes"] for name, metadata in targets.items()}:
        raise ValueError("manifest_targets_invalid")
    if prompt is not None:
        if len(prompt.encode("utf-8")) > 4 * 1024 * 1024:
            raise ValueError("fixture_too_large")
        if hashlib.sha256(prompt.encode()).hexdigest() != manifest["fixture_sha256"]:
            raise ValueError("fixture_hash_mismatch")
        prompt_bytes = prompt.encode("utf-8")
        for name, metadata in targets.items():
            cut = metadata["target_prefix_utf8_bytes"]
            try:
                prefix = prompt_bytes[:cut].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("manifest_target_prefix_invalid") from exc
            suffix = prompt_bytes[cut:].decode("utf-8")
            if not prompt.startswith(prefix) or not suffix.startswith(metadata["value"]):
                raise ValueError("manifest_target_prefix_invalid")
            if scenario == "structured-extraction" and name in {"VII", "XIV", "XXI"}:
                heading = f"Chapter {name}: {STRUCTURED_HEADINGS[name]}\n"
                if not prefix.endswith(heading):
                    raise ValueError("manifest_target_prefix_invalid")
            elif name == "needle" and not prefix.endswith("NEEDLE FACT: "):
                raise ValueError("manifest_target_prefix_invalid")
            elif name == "canary" and not prefix.endswith("RECORD CANARY: "):
                raise ValueError("manifest_target_prefix_invalid")
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

def _aggregate_trial_scores(trials: list[dict[str, Any]]) -> dict[str, Any]:
    exact = sum(1 for t in trials if t.get("exact_match"))
    cats: dict[str, int] = {}
    for t in trials:
        for e in t.get("errors", []): cats[e] = cats.get(e, 0) + 1
    return {"trial_count": len(trials), "exact_match_count": exact, "pass_rate": exact / len(trials) if trials else 0.0, "failure_categories": cats, "trials": trials}

def score_trials(responses: list[str], manifest: dict[str, Any]) -> dict[str, Any]:
    return _aggregate_trial_scores([evaluate_semantic(r, manifest) for r in responses])


GENERATION_OPTION_LIMITS = {
    "max_tokens": (1, 65536),
    "temperature": (0, 2),
    "top_p": (0, 1),
    "seed": (-(2 ** 63), 2 ** 63 - 1),
}
MAX_PACKAGED_TRIALS = 10
CANCELLATION_PHASES = ("prefill", "generating")


def validate_generation_settings(value: Any) -> dict[str, Any]:
    """Validate bounded settings observed at the plaintext envelope boundary."""
    if not isinstance(value, dict) or set(value) != {"supplied", "omitted_runtime_default"}:
        raise ValueError("generation_settings_malformed")
    supplied, omitted = value["supplied"], value["omitted_runtime_default"]
    if not isinstance(supplied, dict) or not isinstance(omitted, list) or not supplied:
        raise ValueError("generation_settings_malformed")
    if not set(supplied) <= set(GENERATION_OPTION_LIMITS) or "max_tokens" not in supplied:
        raise ValueError("generation_settings_unsupported")
    expected_omitted = sorted(set(GENERATION_OPTION_LIMITS) - set(supplied))
    if omitted != expected_omitted:
        raise ValueError("generation_settings_omissions_invalid")
    for key, setting in supplied.items():
        low, high = GENERATION_OPTION_LIMITS[key]
        if (not isinstance(setting, (int, float)) or isinstance(setting, bool)
                or not math.isfinite(setting) or setting < low or setting > high
                or key in {"max_tokens", "seed"} and not isinstance(setting, int)):
            raise ValueError("generation_settings_value_invalid")
    return {"supplied": dict(supplied), "omitted_runtime_default": list(omitted)}


def validate_cancellation_recovery(value: Any, *, cleanup_budget_s: float,
        observation_window_s: float, recovery_timeout_s: float,
        total_prompt_tokens: int, prefill_threshold: int | None = None,
        generation_threshold: int | None = None) -> dict[str, Any]:
    """Validate privacy-safe evidence produced by the physical cancellation sequence."""
    if not isinstance(value, dict) or set(value) != {"scenarios", "operator_lifecycle"}:
        raise ValueError("cancellation_evidence_malformed")
    scenarios = value["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != 2:
        raise ValueError("cancellation_evidence_malformed")
    safe: list[dict[str, Any]] = []
    if (not isinstance(total_prompt_tokens, int) or isinstance(total_prompt_tokens, bool)
            or total_prompt_tokens <= 0):
        raise ValueError("cancellation_prompt_total_invalid")
    required = {"phase", "trigger_observed", "trigger_count", "threshold", "total_prompt_tokens", "attempted",
        "acknowledged", "cleanup_s", "quiescence_s", "stale_progress_count",
        "late_result_count", "active_after_quiescence", "followup_ok", "followup_s"}
    for expected_phase, item in zip(CANCELLATION_PHASES, scenarios):
        if not isinstance(item, dict) or set(item) != required or item.get("phase") != expected_phase:
            raise ValueError("cancellation_evidence_malformed")
        bool_keys = ("trigger_observed", "attempted", "acknowledged",
            "active_after_quiescence", "followup_ok")
        if any(not isinstance(item[key], bool) for key in bool_keys):
            raise ValueError("cancellation_evidence_malformed")
        int_keys = ("trigger_count", "threshold", "total_prompt_tokens", "stale_progress_count", "late_result_count")
        if any(not isinstance(item[key], int) or isinstance(item[key], bool) or item[key] < 0
                for key in int_keys) or item["threshold"] <= 0:
            raise ValueError("cancellation_evidence_malformed")
        configured_threshold = prefill_threshold if expected_phase == "prefill" else generation_threshold
        if configured_threshold is not None and item["threshold"] != configured_threshold:
            raise ValueError("cancellation_threshold_mismatched")
        if item["total_prompt_tokens"] != total_prompt_tokens:
            raise ValueError("cancellation_prompt_total_mismatched")
        for key, bound in (("cleanup_s", cleanup_budget_s),
                ("quiescence_s", observation_window_s + 1), ("followup_s", recovery_timeout_s)):
            if (not isinstance(item[key], (int, float)) or isinstance(item[key], bool)
                    or not math.isfinite(item[key]) or item[key] < 0 or item[key] > bound):
                raise ValueError("cancellation_cleanup_timeout" if key == "cleanup_s"
                    else "cancellation_recovery_timeout")
        if item["quiescence_s"] < observation_window_s:
            raise ValueError("cancellation_evidence_malformed")
        if not item["trigger_observed"] or item["trigger_count"] < item["threshold"]:
            raise ValueError("cancellation_trigger_missed")
        if expected_phase == "prefill" and not (
                0 < item["threshold"] <= item["trigger_count"] < total_prompt_tokens):
            raise ValueError("cancellation_trigger_missed")
        if not item["attempted"] or not item["acknowledged"]:
            raise ValueError("cancellation_unconfirmed")
        if item["late_result_count"]:
            raise ValueError("cancellation_late_result")
        if item["stale_progress_count"] or item["active_after_quiescence"]:
            raise ValueError("cancellation_stale_progress")
        if not item["followup_ok"]:
            raise ValueError("cancellation_followup_failed")
        safe.append(dict(item))
    lifecycle = value["operator_lifecycle"]
    lifecycle_keys = {"stop_confirmed", "restart_ready", "session_changed", "restart_s",
        "post_restart_followup_ok", "post_restart_followup_s"}
    if not isinstance(lifecycle, dict) or set(lifecycle) != lifecycle_keys:
        raise ValueError("cancellation_evidence_malformed")
    if any(not isinstance(lifecycle[key], bool) for key in
            ("stop_confirmed", "restart_ready", "session_changed", "post_restart_followup_ok")):
        raise ValueError("cancellation_evidence_malformed")
    for key in ("restart_s", "post_restart_followup_s"):
        if (not isinstance(lifecycle[key], (int, float)) or isinstance(lifecycle[key], bool)
                or not math.isfinite(lifecycle[key]) or lifecycle[key] < 0
                or lifecycle[key] > recovery_timeout_s):
            raise ValueError("operator_restart_timeout")
    if not lifecycle["stop_confirmed"]:
        raise ValueError("operator_stop_failed")
    if not lifecycle["restart_ready"] or not lifecycle["session_changed"]:
        raise ValueError("operator_restart_failed")
    if not lifecycle["post_restart_followup_ok"]:
        raise ValueError("operator_followup_failed")
    return {"scenarios": safe, "operator_lifecycle": dict(lifecycle), "pass": True}


def prefill_cancellation_trigger_state(processed_prompt_tokens: Any, threshold: Any,
        total_prompt_tokens: Any) -> str:
    """Classify whether observed prefill progress is an interior cancellation point."""
    values = (processed_prompt_tokens, threshold, total_prompt_tokens)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        return "invalid"
    if total_prompt_tokens <= 0 or threshold <= 0 or processed_prompt_tokens < 0:
        return "invalid"
    if threshold >= total_prompt_tokens or processed_prompt_tokens >= total_prompt_tokens:
        return "completed"
    return "trigger" if processed_prompt_tokens >= threshold else "waiting"

def analyze_progress(observations: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Validate the ordered progress/result/terminal lifecycle, returning stable errors."""
    errors: list[str] = []
    progress: list[dict[str, Any]] = []
    terminals: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    last_seq = -1; last_elapsed = -1; last_processed = 0; last_generated = 0
    last_phase = -1; total: int | None = None; terminal_seen = False
    observed_phases: set[str] = set()
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
        observed_phases.add(phase)
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
        if "prefill" not in observed_phases: errors.append("prefill_phase_missing")
        if last_phase != PHASES["generating"]: errors.append("terminal_lifecycle_without_generation")
    return {"pass": not errors, "errors": list(dict.fromkeys(errors)),
        "progress_event_count": len(progress), "first_progress": progress[0] if progress else None,
        "final_progress": progress[-1] if progress else None, "terminal_state": terminal_state,
        "terminal_observation": terminals[0] if len(terminals) == 1 else None,
        "result_observed": len(results) == 1}


def summarize_metrics(*, start_s: float, preparing_end_s: float, prefill_end_s: float,
        first_token_s: float, inference_duration_s: float, request_duration_s: float,
        prompt_tokens: int, output_tokens: int, request_budget_s: float) -> dict[str, Any]:
    """Calculate metrics only within proven monotonic-clock timing domains."""
    numeric = (start_s, preparing_end_s, prefill_end_s, first_token_s,
        inference_duration_s, request_duration_s, request_budget_s)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in numeric):
        return {"pass": False, "code": "timing_non_finite"}
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (prompt_tokens, output_tokens)):
        return {"pass": False, "code": "token_count_invalid"}
    if (request_budget_s <= 0 or inference_duration_s < 0 or request_duration_s < 0
            or not (start_s <= preparing_end_s <= prefill_end_s <= first_token_s)):
        return {"pass": False, "code": "timing_order_invalid"}
    prefill = prefill_end_s - preparing_end_s
    if request_duration_s > request_budget_s:
        return {"pass": False, "code": "request_budget_exceeded"}
    return {"pass": True, "preparing_duration_s": preparing_end_s - start_s,
        "prefill_duration_s": prefill, "time_to_first_token_s": first_token_s - start_s,
        "local_inference_duration_s": inference_duration_s,
        "end_to_end_request_duration_s": request_duration_s,
        "prompt_tokens": prompt_tokens, "output_tokens": output_tokens,
        "prompt_tokens_per_s": prompt_tokens / prefill if prefill > 0 else None,
        "request_budget_s": request_budget_s,
        "completion_margin_s": request_budget_s - request_duration_s,
        "phase_timing_source": "worker_progress_elapsed_ms",
        "inference_timing_source": "parent_inference_monotonic",
        "request_timing_source": "runner_end_to_end_monotonic",
        "completion_token_source": "validated_response_usage"}

def compare_kv_estimate(estimate: dict[str, Any], runtime: dict[str, Any], *,
        backend: str | None = None, context_tokens: int | None = None) -> dict[str, Any]:
    """Compare exact estimator bytes with the pinned diagnostic's rounding interval."""
    estimator_keys = {"profile_id", "backend", "context_size_tokens", "type_k", "type_v",
        "exact_kv_allocation_bytes", "metadata_source", "conservative_fallback_used"}
    runtime_keys = {"method", "llama_cpp_python_version", "llama_cpp_commit", "observed_bytes",
        "precision_bytes", "record_count", "unit", "decimal_places"}
    if not isinstance(estimate, dict) or set(estimate) != estimator_keys:
        return {"pass": False, "code": "kv_estimator_evidence_malformed"}
    if not isinstance(runtime, dict) or set(runtime) != runtime_keys:
        return {"pass": False, "code": "kv_runtime_diagnostic_malformed"}
    integer_fields = [estimate.get("context_size_tokens"), estimate.get("exact_kv_allocation_bytes"),
        runtime.get("observed_bytes"), runtime.get("precision_bytes"), runtime.get("record_count"),
        runtime.get("decimal_places")]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > (1 << 63) - 1
            for value in integer_fields):
        return {"pass": False, "code": "kv_diagnostic_value_invalid"}
    # Bound untrusted diagnostic dimensions before using either in exponentiation or multiplication.
    if runtime["decimal_places"] not in {1, 2} or not 1 <= runtime["record_count"] <= 64:
        return {"pass": False, "code": "kv_diagnostic_provenance_mismatch"}
    profile_id = estimate.get("profile_id")
    profile_type = next((kind for kind in ("f16", "q8", "q4")
        if isinstance(profile_id, str) and kind in profile_id.split("_")), None)
    expected_precision = runtime["record_count"] * math.ceil(
        (1024 * 1024) / (2 * (10 ** runtime["decimal_places"])))
    if (estimate["conservative_fallback_used"] is not False or estimate["exact_kv_allocation_bytes"] <= 0
            or runtime["observed_bytes"] <= 0 or runtime["precision_bytes"] <= 0
            or runtime["record_count"] <= 0 or runtime["method"] != "pinned_llama_cpp_kv_buffer_diagnostic"
            or runtime["llama_cpp_python_version"] != "0.3.32"
            or runtime["llama_cpp_commit"] != "b3fed31b99f9bd37725833674252bccb429bb183"
            or runtime["unit"] != "MiB" or runtime["precision_bytes"] != expected_precision
            or estimate["metadata_source"] != "gguf_header"
            or not isinstance(profile_id, str) or not 1 <= len(profile_id) <= 128
            or estimate["backend"] not in {"cpu", "metal", "cuda"}
            or estimate["type_k"] not in {"f16", "q8", "q4"}
            or estimate["type_v"] not in {"f16", "q8", "q4"}
            or estimate["type_k"] != estimate["type_v"] or profile_type != estimate["type_k"]
            or estimate["context_size_tokens"] != 65536
            or backend is not None and estimate["backend"] != backend
            or context_tokens is not None and estimate["context_size_tokens"] != context_tokens):
        return {"pass": False, "code": "kv_diagnostic_provenance_mismatch"}
    estimated, observed, precision = (estimate["exact_kv_allocation_bytes"],
        runtime["observed_bytes"], runtime["precision_bytes"])
    lower, upper = max(1, observed - precision), observed + precision
    return {"pass": lower <= estimated <= upper, "applicability": "qwen_64k_full",
        "profile_id": profile_id, "backend": estimate["backend"],
        "context_size_tokens": estimate["context_size_tokens"],
        "type_k": estimate["type_k"], "type_v": estimate["type_v"],
        "estimated_bytes": estimated, "observed_bytes": observed,
        "delta_bytes": abs(estimated - observed), "precision_interval_bytes": [lower, upper],
        "precision_bytes": precision, "record_count": runtime["record_count"],
        "decimal_places": runtime["decimal_places"],
        "estimator_provenance": "qwen_selected_profile_gguf_header",
        "runtime_provenance": "pinned_llama_cpp_kv_buffer_diagnostic"}

def validate_kv_applicability(value: Any, *, backend: str, context_tier: str) -> dict[str, Any]:
    keys = {"method", "applicability", "architecture", "profile_id", "backend",
        "context_tier", "context_size_tokens"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("kv_applicability_missing")
    if (value["method"] != "active_runtime_selected_profile"
            or not isinstance(value["architecture"], str) or not 1 <= len(value["architecture"]) <= 64
            or not isinstance(value["profile_id"], str) or not 1 <= len(value["profile_id"]) <= 128
            or value["backend"] != backend or value["context_tier"] != context_tier
            or not isinstance(value["context_size_tokens"], int)
            or isinstance(value["context_size_tokens"], bool)
            or not 1 <= value["context_size_tokens"] <= 65536):
        raise ValueError("kv_applicability_invalid")
    if (value["architecture"] == "qwen3" and context_tier == "64k-full"
            and value["context_size_tokens"] != 65536):
        raise ValueError("kv_applicability_context_mismatch")
    expected = ("qwen_64k_full" if value["architecture"] == "qwen3"
        and context_tier == "64k-full"
        else "not_applicable_verified_non_qwen" if value["architecture"] != "qwen3"
        else "not_applicable_context_tier")
    if value["applicability"] != expected:
        raise ValueError("kv_applicability_mismatch")
    return dict(value)

def validate_kv_comparison_summary(value: Any, *, backend: str | None = None,
        context_tier: str | None = None, context_tokens: int | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("pass") is not True:
        raise ValueError("report_kv_diagnostics_invalid")
    applicability = value.get("applicability")
    if applicability in {"not_applicable_verified_non_qwen", "not_applicable_context_tier"}:
        if set(value) != {"pass", "applicability", "reason", "attestation"} or value["reason"] != applicability:
            raise ValueError("report_kv_diagnostics_invalid")
        attestation = value["attestation"]
        if backend is None or context_tier is None:
            raise ValueError("report_kv_diagnostics_invalid")
        try:
            validated = validate_kv_applicability(attestation, backend=backend, context_tier=context_tier)
        except ValueError as exc:
            raise ValueError("report_kv_diagnostics_invalid") from exc
        if validated["applicability"] != applicability or (context_tokens is not None
                and validated["context_size_tokens"] != context_tokens):
            raise ValueError("report_kv_diagnostics_invalid")
        return dict(value)
    keys = {"pass", "applicability", "profile_id", "backend", "context_size_tokens", "type_k", "type_v",
        "estimated_bytes", "observed_bytes", "delta_bytes", "precision_interval_bytes", "precision_bytes",
        "record_count", "decimal_places", "estimator_provenance", "runtime_provenance", "attestation"}
    if set(value) != keys or applicability != "qwen_64k_full":
        raise ValueError("report_kv_diagnostics_invalid")
    integers = ("context_size_tokens", "estimated_bytes", "observed_bytes", "delta_bytes",
        "precision_bytes", "record_count", "decimal_places")
    integer_limit = (1 << 63) - 1
    if any(not isinstance(value[key], int) or isinstance(value[key], bool)
            or value[key] < 0 or value[key] > integer_limit
            for key in integers):
        raise ValueError("report_kv_diagnostics_invalid")
    lower, upper = value.get("precision_interval_bytes", [None, None]) if isinstance(
        value.get("precision_interval_bytes"), list) and len(value["precision_interval_bytes"]) == 2 else (None, None)
    profile_type = next((kind for kind in ("f16", "q8", "q4")
        if isinstance(value["profile_id"], str) and kind in value["profile_id"].split("_")), None)
    attestation = value.get("attestation")
    try:
        validated_attestation = validate_kv_applicability(attestation,
            backend=value["backend"], context_tier="64k-full")
    except ValueError as exc:
        raise ValueError("report_kv_diagnostics_invalid") from exc
    if (not isinstance(value["profile_id"], str) or not 1 <= len(value["profile_id"]) <= 128
            or value["backend"] not in {"cpu", "metal", "cuda"} or value["type_k"] not in {"f16", "q8", "q4"}
            or value["type_k"] != value["type_v"] or profile_type != value["type_k"]
            or value["context_size_tokens"] != 65536
            or backend is not None and value["backend"] != backend
            or context_tier is not None and context_tier != "64k-full"
            or context_tokens is not None and value["context_size_tokens"] != context_tokens
            or validated_attestation["applicability"] != "qwen_64k_full"
            or validated_attestation["profile_id"] != value["profile_id"]
            or value["record_count"] <= 0 or value["record_count"] > 64
            or value["decimal_places"] not in {1, 2}
            or value["delta_bytes"] != abs(value["estimated_bytes"] - value["observed_bytes"])
            or lower != max(1, value["observed_bytes"] - value["precision_bytes"])
            or upper != value["observed_bytes"] + value["precision_bytes"]
            or not lower <= value["estimated_bytes"] <= upper
            or value["precision_bytes"] != value["record_count"] * math.ceil(
                (1024 * 1024) / (2 * 10 ** value["decimal_places"]))
            or value["estimator_provenance"] != "qwen_selected_profile_gguf_header"
            or value["runtime_provenance"] != "pinned_llama_cpp_kv_buffer_diagnostic"):
        raise ValueError("report_kv_diagnostics_invalid")
    return dict(value)

def sanitize(value: Any) -> Any:
    if isinstance(value, dict): return {str(k)[:64]: sanitize(v) for k,v in value.items() if str(k).lower() not in SENSITIVE_KEYS}
    if isinstance(value, list): return [sanitize(v) for v in value[:100]]
    if isinstance(value, str):
        s = value[:512]
        for pat in SECRET_PATTERNS: s = pat.sub("<redacted>", s)
        return s
    return value

RUNTIME_CONFIGURATION_KEYS = {
    "mode", "backend", "context", "runtime_profile", "batch_profile", "kv_cache",
    "acceleration", "yarn_rope",
}
NOT_APPLICABLE_CONFIGURATION = {"status": "not_applicable", "reason": "not_qwen_64k_profile"}


def _configuration_int(value: Any, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError("runtime_configuration_invalid")
    return value


def _configuration_number(value: Any, *, minimum: float = 0.0, maximum: float = 65536.0) -> float:
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or not minimum <= float(value) <= maximum):
        raise ValueError("runtime_configuration_invalid")
    return float(value)


def _configuration_identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.+:-]{1,128}", value):
        raise ValueError("runtime_configuration_invalid")
    return value


def validate_runtime_configuration(value: Any, *, backend: str, context_tier: str,
        context_tokens: int, kv_attestation: dict[str, Any]) -> dict[str, Any]:
    """Validate the exact, privacy-safe current-worker configuration attestation."""
    applicability = kv_attestation.get("attestation")
    if (not isinstance(applicability, dict)
            or applicability.get("applicability") not in {
                "qwen_64k_full", "not_applicable_verified_non_qwen",
                "not_applicable_context_tier"}
            or not isinstance(value, dict) or set(value) != RUNTIME_CONFIGURATION_KEYS):
        raise ValueError("runtime_configuration_invalid")
    mode = value["mode"]
    expected_mode = {"requested": "cpu", "effective": "cpu"} if backend == "cpu" else {
        "requested": "gpu", "effective": backend}
    if (not isinstance(mode, dict) or set(mode) != {"requested", "effective"}
            or mode != expected_mode):
        raise ValueError("runtime_configuration_invalid")
    backend_evidence = value["backend"]
    if (not isinstance(backend_evidence, dict)
            or set(backend_evidence) != {"requested", "available", "selected", "used", "fallback_reason"}
            or any(backend_evidence[key] not in {"cpu", "metal", "cuda"}
                for key in ("requested", "available", "selected", "used"))
            or any(backend_evidence[key] != backend
                for key in ("requested", "available", "selected", "used"))
            or backend_evidence["fallback_reason"] != "none"):
        raise ValueError("runtime_configuration_invalid")
    context = value["context"]
    if (not isinstance(context, dict) or set(context) != {"tier", "effective_window_tokens"}
            or context["tier"] != context_tier
            or _configuration_int(context["effective_window_tokens"], minimum=1, maximum=131072) != context_tokens):
        raise ValueError("runtime_configuration_invalid")

    profile = value["runtime_profile"]
    batch = value["batch_profile"]
    kv_cache = value["kv_cache"]
    acceleration = value["acceleration"]
    applicable = applicability.get("applicability") == "qwen_64k_full"
    if not applicable:
        if any(section != NOT_APPLICABLE_CONFIGURATION
                for section in (profile, batch, kv_cache, acceleration, value["yarn_rope"])):
            raise ValueError("runtime_configuration_invalid")
        return value
    if profile == NOT_APPLICABLE_CONFIGURATION:
        raise ValueError("runtime_configuration_invalid")
    else:
        if (not isinstance(profile, dict) or set(profile) != {
                "selected", "preferred", "attempted", "recovery_count", "result", "fallback_reason"}
                or not isinstance(profile["attempted"], list) or not 1 <= len(profile["attempted"]) <= 16
                or profile["selected"] not in profile["attempted"]
                or profile["result"] != "passed"
                or profile["fallback_reason"] not in {
                    "none", "memory_pressure", "compatibility_failure",
                    "capability_incompatibility"}):
            raise ValueError("runtime_configuration_invalid")
        for identifier in [profile["selected"], profile["preferred"], *profile["attempted"]]:
            _configuration_identifier(identifier)
        if _configuration_int(profile["recovery_count"], maximum=15) != len(profile["attempted"]) - 1:
            raise ValueError("runtime_configuration_invalid")
        if (not isinstance(batch, dict) or set(batch) != {"requested", "selected", "n_batch", "n_ubatch"}
                or batch["requested"] not in {"safe", "balanced", "experimental"}
                or batch["selected"] not in {"safe", "balanced", "experimental"}):
            raise ValueError("runtime_configuration_invalid")
        _configuration_int(batch["n_batch"], minimum=1, maximum=65536)
        _configuration_int(batch["n_ubatch"], minimum=1, maximum=65536)
        if (not isinstance(kv_cache, dict) or set(kv_cache) != {"precision", "type_k", "type_v", "device"}
                or kv_cache["precision"] not in {"f16", "q8", "q4"}
                or kv_cache["device"] not in {"cpu", "metal", "cuda"}):
            raise ValueError("runtime_configuration_invalid")
        _configuration_int(kv_cache["type_k"], maximum=64)
        _configuration_int(kv_cache["type_v"], maximum=64)
        kv_type_ids = {"f16": 1, "q8": 8, "q4": 2}
        if (profile["selected"] != applicability.get("profile_id")
                or kv_cache["precision"] != kv_attestation.get("type_k")
                or kv_attestation.get("type_k") != kv_attestation.get("type_v")
                or kv_cache["type_k"] != kv_type_ids.get(kv_attestation.get("type_k"))
                or kv_cache["type_v"] != kv_type_ids.get(kv_attestation.get("type_v"))):
            raise ValueError("runtime_configuration_invalid")
        if (not isinstance(acceleration, dict)
                or set(acceleration) != {"flash_attention", "kqv_offload", "offloaded_layers"}
                or not isinstance(acceleration["flash_attention"], bool)
                or not isinstance(acceleration["kqv_offload"], bool)
                or (acceleration["offloaded_layers"] != "all_supported_layers"
                    and (not isinstance(acceleration["offloaded_layers"], int)
                         or isinstance(acceleration["offloaded_layers"], bool)
                         or not 0 <= acceleration["offloaded_layers"] <= 10000))):
            raise ValueError("runtime_configuration_invalid")

    yarn = value["yarn_rope"]
    if yarn == NOT_APPLICABLE_CONFIGURATION:
        raise ValueError("runtime_configuration_invalid")
    if not isinstance(yarn, dict) or set(yarn) != {
            "requested_context_tokens", "original_context_tokens", "context_multiplier",
            "rope_frequency_scale", "extension_factor_overridden", "scaling_source",
            "configuration_valid"}:
        raise ValueError("runtime_configuration_invalid")
    requested = _configuration_int(yarn["requested_context_tokens"], minimum=1, maximum=131072)
    original = _configuration_int(yarn["original_context_tokens"], minimum=1, maximum=131072)
    multiplier = _configuration_number(yarn["context_multiplier"], minimum=0.000001, maximum=16)
    frequency = _configuration_number(yarn["rope_frequency_scale"], minimum=0.000001, maximum=16)
    if (not isinstance(yarn["extension_factor_overridden"], bool)
            or yarn["scaling_source"] not in {"not_required", "top_level_enum", "nested_enum", "llama_class_enum", "numeric_fallback"}
            or yarn["configuration_valid"] is not True
            or requested != context_tokens):
        raise ValueError("runtime_configuration_invalid")
    if context_tier == "64k-full" and applicability.get("architecture") == "qwen3":
        if not (requested == 65536 and original == 32768 and math.isclose(multiplier, 2.0)
                and math.isclose(frequency, 0.5) and yarn["extension_factor_overridden"] is False
                and yarn["scaling_source"] != "not_required"):
            raise ValueError("runtime_configuration_invalid")
    return value

def validate_report(report: Any) -> None:
    """Validate the stable, privacy-safe v2 report envelope before replacement."""
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
        timeout_fields = {"last_safe_phase", "request_timeout_s", "setup_timeout_s",
            "finalization_timeout_s", "cancellation_timeout_s", "cleanup_timeout_s", "runner_timeout_s",
            "overall_timeout_s", "elapsed_s", "cleanup_succeeded"}
        present_timeout_fields = timeout_fields.intersection(report)
        if report["code"] == "packaged_runner_timeout":
            if (present_timeout_fields != timeout_fields
                    or not isinstance(report["last_safe_phase"], str)
                    or report["last_safe_phase"] not in PACKAGED_PHASES
                    or not all(finite(report[key]) and report[key] >= 0 for key in timeout_fields
                        - {"last_safe_phase", "cleanup_succeeded"})
                    or not isinstance(report["cleanup_succeeded"], bool)
                    or report["runner_timeout_s"] != report["setup_timeout_s"]
                        + report["request_timeout_s"] + report["finalization_timeout_s"]
                        + report["cancellation_timeout_s"]
                    or report["overall_timeout_s"] != report["runner_timeout_s"]
                        + report["cleanup_timeout_s"]
                    or report["elapsed_s"] > report["overall_timeout_s"]):
                raise ValueError("report_timeout_diagnostics_invalid")
        elif report["code"] == "packaged_runner_failed":
            failure_fields = {"last_safe_phase", "failure_reason", "elapsed_s", "cleanup_succeeded"}
            if (failure_fields.intersection(report) != failure_fields
                    or present_timeout_fields != failure_fields - {"failure_reason"}
                    or not isinstance(report["last_safe_phase"], str)
                    or report["last_safe_phase"] not in PACKAGED_PHASES
                    or not isinstance(report["failure_reason"], str)
                    or report["failure_reason"] not in PACKAGED_FAILURE_REASONS
                    or not finite(report["elapsed_s"]) or report["elapsed_s"] < 0
                    or not isinstance(report["cleanup_succeeded"], bool)):
                raise ValueError("report_runner_failure_diagnostics_invalid")
        elif present_timeout_fields:
            raise ValueError("report_timeout_diagnostics_unexpected")
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
    local_progress = report.get("authoritative_local_progress")
    progress, metrics, semantic = report.get("encrypted_progress"), report.get("metrics"), report.get("semantic")
    if (not isinstance(local_progress, dict) or set(local_progress) != {
            "pass", "progress_event_count", "observed_phases"}
            or local_progress.get("pass") is not True
            or not isinstance(local_progress.get("progress_event_count"), int)):
        raise ValueError("report_authoritative_local_progress_invalid")
    if not isinstance(progress, dict) or progress.get("pass") is not True or not isinstance(progress.get("progress_event_count"), int):
        raise ValueError("report_progress_invalid")
    usage = report.get("response_usage")
    if (not isinstance(usage, dict) or set(usage) != {
            "prompt_tokens", "completion_tokens", "finish_reason", "source"}
            or usage.get("source") != "validated_atomic_response_usage"
            or any(not isinstance(usage.get(key), int) or isinstance(usage.get(key), bool)
                or usage[key] <= 0 for key in ("prompt_tokens", "completion_tokens"))
            or not isinstance(usage.get("finish_reason"), str) or not usage["finish_reason"]):
        raise ValueError("report_response_usage_invalid")
    if (usage["prompt_tokens"] != context["prompt_tokens"]
            or usage["completion_tokens"] != context["output_tokens"]):
        raise ValueError("report_response_usage_invalid")
    if report.get("atomic_response_completion") != {"completed": True,
            "source": "browser_decrypted_final_response"}:
        raise ValueError("report_atomic_response_invalid")
    if report.get("post_terminal_silence") != {"observed": True,
            "source": "pre_cancellation_primary_snapshot"}:
        raise ValueError("report_post_terminal_silence_invalid")
    metric_keys = ("preparing_duration_s", "prefill_duration_s", "time_to_first_token_s",
        "local_inference_duration_s", "end_to_end_request_duration_s", "prompt_tokens",
        "output_tokens", "request_budget_s", "completion_margin_s")
    if not isinstance(metrics, dict) or metrics.get("pass") is not True or not all(finite(metrics.get(key)) for key in metric_keys):
        raise ValueError("report_metrics_invalid")
    for key in ("prompt_tokens_per_s",):
        if key not in metrics or (metrics[key] is not None and not finite(metrics[key])):
            raise ValueError("report_metrics_invalid")
    if any(metrics.get(key) != value for key, value in {
            "phase_timing_source": "worker_progress_elapsed_ms",
            "inference_timing_source": "parent_inference_monotonic",
            "request_timing_source": "runner_end_to_end_monotonic",
            "completion_token_source": "validated_response_usage"}.items()):
        raise ValueError("report_metrics_invalid")
    if not isinstance(semantic, dict) or not isinstance(semantic.get("semantic_pass"), bool):
        raise ValueError("report_semantic_invalid")
    aggregate = report.get("aggregate_semantic")
    if not isinstance(aggregate, dict) or not isinstance(aggregate.get("trial_count"), int) or \
            not isinstance(aggregate.get("exact_match_count"), int) or not finite(aggregate.get("pass_rate")):
        raise ValueError("report_semantic_aggregate_invalid")
    requested, completed = report.get("requested_trial_count"), report.get("completed_trial_count")
    if (not isinstance(requested, int) or not 1 <= requested <= MAX_PACKAGED_TRIALS
            or completed != requested or aggregate["trial_count"] != completed
            or not 0 <= aggregate["exact_match_count"] <= completed
            or aggregate["pass_rate"] != aggregate["exact_match_count"] / completed
            or not isinstance(aggregate.get("failure_categories"), dict)
            or not isinstance(aggregate.get("trials"), list)
            or len(aggregate["trials"]) != completed):
        raise ValueError("report_trial_aggregate_invalid")
    validate_generation_settings(report.get("generation_settings"))
    memory = report.get("memory")
    if (not isinstance(memory, dict) or set(memory) != {"maximum_peak_rss_bytes", "trials"}
            or not isinstance(memory["maximum_peak_rss_bytes"], int)
            or isinstance(memory["maximum_peak_rss_bytes"], bool)
            or memory["maximum_peak_rss_bytes"] < 0
            or not isinstance(memory["trials"], list) or len(memory["trials"]) != completed):
        raise ValueError("report_memory_invalid")
    trial_memory = [validate_physical_memory_evidence(item) for item in memory["trials"]]
    if memory["maximum_peak_rss_bytes"] != max(item["peak_rss_bytes"] for item in trial_memory):
        raise ValueError("report_memory_invalid")
    if "cancellation_recovery" in report:
        cancellation = report["cancellation_recovery"]
        if (not isinstance(cancellation, dict) or cancellation.get("pass") is not True
                or not isinstance(cancellation.get("scenarios"), list)
                or len(cancellation["scenarios"]) != 2
                or not isinstance(cancellation.get("operator_lifecycle"), dict)):
            raise ValueError("report_cancellation_recovery_invalid")
    kv = report.get("kv_diagnostics")
    if (not isinstance(kv, dict) or set(kv) != {"trials"}
            or not isinstance(kv["trials"], list) or len(kv["trials"]) != completed):
        raise ValueError("report_kv_diagnostics_invalid")
    configuration = report.get("runtime_configuration")
    if (not isinstance(configuration, dict) or set(configuration) != {"trials"}
            or not isinstance(configuration["trials"], list)
            or len(configuration["trials"]) != completed):
        raise ValueError("report_runtime_configuration_invalid")
    validated_configurations = []
    for item, config in zip(kv["trials"], configuration["trials"]):
        summary = validate_kv_comparison_summary(item, backend=backend["used"],
            context_tier=context["tier"], context_tokens=context["window_tokens"])
        validated_configurations.append(validate_runtime_configuration(config,
            backend=backend["used"], context_tier=context["tier"],
            context_tokens=context["window_tokens"], kv_attestation=summary))
    if any(item != validated_configurations[0] for item in validated_configurations[1:]):
        raise ValueError("report_runtime_configuration_drift")

def write_report_atomic(out_dir: Path, report: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = sanitize({"schema_version": SCHEMA_VERSION, **report})
    validate_report(report)
    text = _canonical_json(report) + "\n"
    fd, name = tempfile.mkstemp(prefix=".long-context-report-", suffix=".json", dir=out_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(text)
    dest = out_dir / "long_context_benchmark_report.json"
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

MEMORY_METHOD = "psutil_process_tree_rss_v1"
MEMORY_SCOPE = "owned_tauri_driver_process_tree"

def normalized_memory_platform(system: str | None = None) -> str:
    value = (system or platform.system()).lower()
    return {"darwin": "macos", "linux": "linux", "windows": "windows"}.get(value, "unsupported")

class OwnedProcessTreeMemorySampler:
    """Aggregate RSS only for a runner-owned root and its current descendants."""
    def __init__(self, root_pid: int, process_factory: Callable[[int], Any] = psutil.Process,
            system: str | None = None):
        self.root_pid = root_pid
        self.process_factory = process_factory
        self.platform = normalized_memory_platform(system)
        self.samples: list[int] = []

    def sample(self) -> bool:
        if self.platform == "unsupported":
            return False
        try:
            root = self.process_factory(self.root_pid)
            processes = [root, *root.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False
        rss = 0
        observed = 0
        for process in processes:
            try:
                value = process.memory_info().rss
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    rss += value
                    observed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        if not observed:
            return False
        self.samples.append(rss)
        return True

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            raise ValueError("memory_sample_unavailable")
        return {"method": MEMORY_METHOD, "scope": MEMORY_SCOPE, "platform": self.platform,
            "sample_count": len(self.samples), "baseline_rss_bytes": self.samples[0],
            "peak_rss_bytes": max(self.samples), "final_rss_bytes": self.samples[-1]}

def validate_physical_memory_evidence(value: Any) -> dict[str, Any]:
    keys = {"method", "scope", "platform", "sample_count", "baseline_rss_bytes",
        "peak_rss_bytes", "final_rss_bytes"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("physical_memory_evidence_invalid")
    if value["method"] != MEMORY_METHOD or value["scope"] != MEMORY_SCOPE or \
            value["platform"] not in {"linux", "macos", "windows"}:
        raise ValueError("physical_memory_evidence_invalid")
    numeric = ("sample_count", "baseline_rss_bytes", "peak_rss_bytes", "final_rss_bytes")
    if any(not isinstance(value[key], int) or isinstance(value[key], bool) for key in numeric):
        raise ValueError("physical_memory_evidence_invalid")
    if (value["sample_count"] <= 0 or value["baseline_rss_bytes"] < 0
            or value["final_rss_bytes"] < 0
            or value["peak_rss_bytes"] < value["baseline_rss_bytes"]
            or value["peak_rss_bytes"] < value["final_rss_bytes"]):
        raise ValueError("physical_memory_evidence_invalid")
    return dict(value)

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


def benchmark_operator_mode(backend: str) -> str:
    """Map an attested backend to the operator control value."""
    if backend == "cpu":
        return "cpu"
    if backend in {"metal", "cuda"}:
        return "gpu"
    raise ValueError("unsupported long-context benchmark backend")


def apply_benchmark_context_tier(driver: object, context_tier: str) -> str:
    """Set and read back the landing-page tier through the browser boundary."""
    return driver.execute_script(
        "const v=document.querySelector('#app').__vue__; v.selectedContextTier=arguments[0]; "
        "v.persistContextTier(arguments[0]); return v.selectedContextTier;", context_tier)


def classify_benchmark_landing_state(state: object) -> tuple[str, str | None]:
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


def _is_windows_sharing_violation(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in {32, 33}


def _read_packaged_phase_status(path: Path, parent_elapsed_s: float, *, final: bool = False) -> tuple[dict[str, Any] | None, str | None]:
    """Read one complete checkpoint; sharing denials and partial JSON are retryable misses."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "packaged_phase_status_missing"
    except json.JSONDecodeError:
        return None, ("packaged_phase_status_malformed" if final
            else "packaged_phase_status_missing")
    except OSError as exc:
        if _is_windows_sharing_violation(exc):
            return None, "packaged_phase_status_missing"
        raise
    if (not isinstance(value, dict) or set(value) != {"schema_version", "phase", "sequence",
            "last_safe_phase", "failure_reason", "elapsed_s", "cleanup_succeeded"}
            or value.get("schema_version") != PACKAGED_PHASE_STATUS_VERSION
            or value.get("phase") not in PACKAGED_PHASES
            or value.get("last_safe_phase") not in PACKAGED_PHASES
            or not (isinstance(value.get("failure_reason"), str)
                or value.get("failure_reason") is None)
            or value.get("failure_reason") not in PACKAGED_FAILURE_REASONS | {None}
            or not (isinstance(value.get("cleanup_succeeded"), bool)
                or value.get("cleanup_succeeded") is None)
            or value.get("cleanup_succeeded") not in {True, False, None}
            or not isinstance(value.get("sequence"), int) or isinstance(value.get("sequence"), bool)
            or value["sequence"] != PACKAGED_PHASES.index(value["phase"]) + 1
            or not isinstance(value.get("elapsed_s"), (int, float))
            or isinstance(value.get("elapsed_s"), bool) or not math.isfinite(value["elapsed_s"])
            or value["elapsed_s"] < 0 or value["elapsed_s"] > parent_elapsed_s + 1.0):
        return None, "packaged_phase_status_malformed"
    if final and (value["phase"] != "cleanup"
            or not isinstance(value["cleanup_succeeded"], bool)):
        return None, "packaged_phase_status_malformed"
    return value, None


def _run_owned_runner(command: list[str], timeout_s: float,
        cleanup_timeout_s: float, *, popen: Callable[..., Any] = subprocess.Popen,
        cleanup_run: Callable[..., Any] = subprocess.run,
        killpg: Callable[[int, int], Any] | None = None,
        platform_name: str | None = None, phase_status_path: Path | None = None,
        clock: Callable[[], float] | None = None,
        phase_poll_interval_s: float = 0.05) -> subprocess.CompletedProcess[str]:
    """Run one owned process group without buffering output or killing by name."""
    kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    clock = time.monotonic if clock is None else clock
    owned_platform = os.name if platform_name is None else platform_name
    if owned_platform == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    else:
        kwargs["start_new_session"] = True
    process = popen(command, **kwargs)  # noqa: S603
    started = clock()
    work_deadline = started + timeout_s
    overall_deadline = work_deadline + cleanup_timeout_s
    active_deadline = work_deadline
    cleanup_observed = False
    returncode: int | None = None
    chunks: deque[bytes] = deque(maxlen=8)
    def drain_output() -> None:
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read(256), b""):
            chunks.append(chunk)
    drain = threading.Thread(target=drain_output, daemon=True)
    drain.start()
    while True:
        now = clock()
        if now >= active_deadline:
            break
        # A bounded wait doubles as the phase monitor: it avoids polling sleeps
        # while ensuring an atomic cleanup checkpoint is observed promptly.
        monitor_window_s = min(phase_poll_interval_s, active_deadline - now)
        try:
            returncode = process.wait(timeout=monitor_window_s)
            break
        except subprocess.TimeoutExpired:
            elapsed_s = max(0.0, clock() - started)
            phase = None
            if phase_status_path is not None:
                status, _phase_error = _read_packaged_phase_status(phase_status_path, elapsed_s)
                phase = status.get("phase") if status else None
            if phase == "cleanup" and not cleanup_observed:
                cleanup_observed = True
                active_deadline = min(overall_deadline, clock() + cleanup_timeout_s)
    if returncode is None:
        cleanup_succeeded = False
        cleanup_deadline = active_deadline if cleanup_observed else overall_deadline
        def cleanup_remaining() -> float:
            return max(0.001, cleanup_deadline - clock())
        if owned_platform == "nt":
            taskkill_ok = False
            try:
                killed = cleanup_run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=cleanup_remaining(), check=False)  # noqa: S603
                taskkill_ok = getattr(killed, "returncode", 1) == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
            if not taskkill_ok:
                process.kill()
            try:
                process.wait(timeout=cleanup_remaining())
                cleanup_succeeded = taskkill_ok
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=cleanup_remaining())
        else:
            owned_killpg = killpg if killpg is not None else getattr(os, "killpg", None)
            if not callable(owned_killpg):
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=cleanup_remaining())
                raise RuntimeError("owned_process_group_cleanup_unavailable") from None
            with contextlib.suppress(ProcessLookupError):
                owned_killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=cleanup_remaining())
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    owned_killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=cleanup_remaining())
            # The leader can exit before its browser/driver descendants. Target
            # the whole owned group again and prove it is gone before claiming
            # successful cleanup in the bounded timeout diagnostic.
            with contextlib.suppress(ProcessLookupError):
                owned_killpg(process.pid, signal.SIGKILL)
            while clock() < cleanup_deadline:
                try:
                    owned_killpg(process.pid, 0)
                except ProcessLookupError:
                    cleanup_succeeded = True
                    break
                time.sleep(min(0.01, cleanup_remaining()))
        raise PackagedRunnerTimeout(command, timeout_s, cleanup_succeeded) from None
    drain.join(timeout=cleanup_timeout_s)
    tail = b"".join(chunks)[-2048:].decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(command, returncode, stdout=tail)


def invoke_packaged_runtime_adapter(*, fixture_id: str = "small-8k", scenario: str = "structured-extraction", timeout_s: float = 30.0,
        model: str | None = None, backend: str | None = None, relay_url: str | None = None,
        cleanup_timeout_s: float | None = None, app_binary: str | None = None,
        context_tier: str = "64k-full", report_only: bool = False,
        external_prompt: str | None = None, external_manifest: Any | None = None,
        subprocess_run: Callable[..., Any] | None = None, cancellation_validation: bool = False,
        prefill_cancel_tokens: int | None = None, prefill_cancel_fraction: float | None = None,
        generation_cancel_tokens: int = 1, observation_window_s: float = 0.5,
        recovery_timeout_s: float = 30.0) -> dict[str, Any]:
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
    if cancellation_validation:
        cancel_numbers = (generation_cancel_tokens, observation_window_s, recovery_timeout_s)
        if (not isinstance(generation_cancel_tokens, int) or isinstance(generation_cancel_tokens, bool)
                or not 1 <= generation_cancel_tokens <= 65536
                or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                    or not math.isfinite(v) or v <= 0 or v > 300 for v in cancel_numbers[1:])
                or (prefill_cancel_tokens is None) == (prefill_cancel_fraction is None)
                or prefill_cancel_tokens is not None and (not isinstance(prefill_cancel_tokens, int)
                    or isinstance(prefill_cancel_tokens, bool) or prefill_cancel_tokens <= 0)
                or prefill_cancel_fraction is not None and (not isinstance(prefill_cancel_fraction, float)
                    or not math.isfinite(prefill_cancel_fraction) or not 0 < prefill_cancel_fraction < 1)):
            return {"pass": False, "code": "cancellation_configuration_invalid"}
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
    cancellation_budget_s = (packaged_cancellation_budget_s(float(timeout_s),
        float(observation_window_s), float(recovery_timeout_s)) if cancellation_validation else 0.0)
    runner_budget_s = (PACKAGED_SETUP_BUDGET_S + float(timeout_s)
        + PACKAGED_FINALIZATION_BUDGET_S + cancellation_budget_s)
    overall_budget_s = runner_budget_s + float(cleanup_timeout_s)
    request = {"fixture_id": fixture_id, "prompt": prompt, "manifest": manifest,
        "model": str(model_path), "backend": backend, "relay_url": relay_url,
        "context_tier": context_tier, "request_timeout_s": timeout_s,
        "cleanup_timeout_s": cleanup_timeout_s, "setup_timeout_s": PACKAGED_SETUP_BUDGET_S,
        "finalization_timeout_s": PACKAGED_FINALIZATION_BUDGET_S,
        "cancellation_timeout_s": cancellation_budget_s,
        "phase_status_version": PACKAGED_PHASE_STATUS_VERSION,
        "phase_status_phases": list(PACKAGED_PHASES),
        "cancellation_validation": cancellation_validation,
        "cancellation": {"prefill_tokens": prefill_cancel_tokens,
            "prefill_fraction": prefill_cancel_fraction, "generation_tokens": generation_cancel_tokens,
            "observation_window_s": observation_window_s, "recovery_timeout_s": recovery_timeout_s}}
    request_name = evidence_name = diagnostic_name = phase_name = None
    runner_started = time.monotonic()
    try:
        request_fd, request_name = tempfile.mkstemp(prefix="long-context-request-", suffix=".json")
        evidence_fd, evidence_name = tempfile.mkstemp(prefix="long-context-evidence-", suffix=".json")
        phase_fd, phase_name = tempfile.mkstemp(prefix="long-context-phase-", suffix=".json")
        if hasattr(os, "fchmod"):
            os.fchmod(request_fd, 0o600); os.fchmod(evidence_fd, 0o600); os.fchmod(phase_fd, 0o600)
        with os.fdopen(request_fd, "w", encoding="utf-8") as handle:
            json.dump(request, handle)
        os.close(evidence_fd)
        os.close(phase_fd)
        command = [sys.executable, str(Path(__file__).parents[2] / "desktop-tauri" / "scripts" /
            "test_desktop_operator_ui_e2e.py"), "--benchmark-request", request_name,
            "--benchmark-evidence", evidence_name, "--benchmark-phase-status", phase_name,
            "--app-binary", str(app_path)]
        diagnostic_fd, diagnostic_name = tempfile.mkstemp(prefix="long-context-runner-", suffix=".log")
        try:
            with os.fdopen(diagnostic_fd, "w+", encoding="utf-8") as diagnostic_handle:
                if subprocess_run is None:
                    completed = _run_owned_runner(command, runner_budget_s, cleanup_timeout_s,
                        phase_status_path=Path(phase_name))
                else:
                    completed = subprocess_run(command, stdout=diagnostic_handle,
                        stderr=subprocess.STDOUT, text=True,
                        timeout=overall_budget_s, check=False)
        except subprocess.TimeoutExpired as exc:
            elapsed_s = min(overall_budget_s, max(0.0, time.monotonic() - runner_started))
            # A watchdog timeout may only have an active-phase checkpoint.  The
            # watchdog independently owns and reports cleanup in this path.
            status, phase_error = _read_packaged_phase_status(Path(phase_name), elapsed_s)
            if phase_error:
                return {"pass": False, "runtime_contract_pass": False, "code": phase_error}
            return {"pass": False, "runtime_contract_pass": False,
                "code": "packaged_runner_timeout", "last_safe_phase": status["last_safe_phase"],
                "request_timeout_s": float(timeout_s),
                "setup_timeout_s": PACKAGED_SETUP_BUDGET_S,
                "finalization_timeout_s": PACKAGED_FINALIZATION_BUDGET_S,
                "cancellation_timeout_s": cancellation_budget_s,
                "cleanup_timeout_s": float(cleanup_timeout_s),
                "runner_timeout_s": runner_budget_s, "overall_timeout_s": overall_budget_s,
                "elapsed_s": elapsed_s,
                "cleanup_succeeded": bool(getattr(exc, "cleanup_succeeded", False))}
        if completed.returncode != 0:
            elapsed_s = min(overall_budget_s, max(0.0, time.monotonic() - runner_started))
            status, phase_error = _read_packaged_phase_status(Path(phase_name), elapsed_s, final=True)
            if phase_error:
                return {"pass": False, "runtime_contract_pass": False, "code": phase_error}
            return {"pass": False, "runtime_contract_pass": False,
                "code": "packaged_runner_failed", "last_safe_phase": status["last_safe_phase"],
                "failure_reason": status["failure_reason"] or (
                    "cleanup_failure" if not status["cleanup_succeeded"]
                    else "packaged_runner_failure"),
                "elapsed_s": min(runner_budget_s, float(status["elapsed_s"])),
                "cleanup_succeeded": status["cleanup_succeeded"] is True}
        elapsed_s = min(overall_budget_s, max(0.0, time.monotonic() - runner_started))
        status, phase_error = _read_packaged_phase_status(Path(phase_name), elapsed_s, final=True)
        if phase_error:
            return {"pass": False, "runtime_contract_pass": False,
                "code": phase_error}
        if not status["cleanup_succeeded"]:
            return {"pass": False, "runtime_contract_pass": False,
                "code": "packaged_runner_failed", "last_safe_phase": status["last_safe_phase"],
                "failure_reason": status["failure_reason"] or "cleanup_failure",
                "elapsed_s": min(runner_budget_s, float(status["elapsed_s"])),
                "cleanup_succeeded": False}
        try:
            payload = json.loads(Path(evidence_name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"pass": False, "code": "packaged_evidence_malformed"}
    finally:
        for name in (request_name, evidence_name, diagnostic_name, phase_name):
            if name:
                Path(name).unlink(missing_ok=True)
    if not isinstance(payload, dict):
        return {"pass": False, "code": "packaged_evidence_malformed"}
    required = {"app_identity", "runtime_identity", "bundled_runtime_identity", "build_identity",
        "backend_requested", "backend_selected", "backend_used", "model_fingerprint",
        "authoritative_prompt_tokens", "local_telemetry", "progress_events",
        "authoritative_tokenizer_evidence", "atomic_response_completed", "response_metadata",
        "response_text", "request_duration_s", "post_terminal_observations", "generation_settings", "memory",
        "runtime_configuration"}
    if payload and payload.get("local_telemetry") in (None, {}):
        return {"pass": False, "runtime_contract_pass": False,
            "code": "authoritative_local_progress_missing"}
    if payload and payload.get("response_metadata") in (None, {}):
        return {"pass": False, "runtime_contract_pass": False,
            "code": "response_usage_missing_or_inconsistent"}
    missing_evidence = sorted(key for key in required if key not in payload or payload.get(key) in (None, "", {}))
    if missing_evidence:
        if "authoritative_tokenizer_evidence" in missing_evidence:
            return {"pass": False, "code": "authoritative_target_depth_unavailable",
                "missing_seam": "packaged_admission_render_and_tokenize_chat_prefix_counts"}
        return {"pass": False, "code": "packaged_evidence_missing", "missing": missing_evidence}
    if (not isinstance(payload["authoritative_prompt_tokens"], int) or
            isinstance(payload["authoritative_prompt_tokens"], bool) or
            payload["authoritative_prompt_tokens"] <= 0 or
            not isinstance(payload["progress_events"], list) or
            not isinstance(payload["post_terminal_observations"], list)):
        return {"pass": False, "code": "packaged_evidence_malformed"}
    try:
        generation_settings = validate_generation_settings(payload["generation_settings"])
        memory = validate_physical_memory_evidence(payload["memory"])
    except ValueError as exc:
        return {"pass": False, "runtime_contract_pass": False, "code": str(exc)}
    authoritative_offsets, depth_error = _validate_authoritative_tokenizer_evidence(
        payload["authoritative_tokenizer_evidence"], manifest,
        payload["runtime_identity"], payload["authoritative_prompt_tokens"])
    if depth_error:
        return {"pass": False, "code": depth_error}
    try:
        requested_max_tokens = generation_settings["supplied"].get("max_tokens")
        if requested_max_tokens != profile.default_output_reservation_tokens:
            raise ValueError("local_telemetry_configuration_mismatch")
        local = validate_authoritative_local_telemetry(payload["local_telemetry"],
            expected_tier=context_tier,
            expected_output_reservation=requested_max_tokens)
        progress = validate_encrypted_progress_delivery(payload["progress_events"], local)
    except ValueError as exc:
        return {"pass": False, "runtime_contract_pass": False, "code": str(exc)}
    metadata = payload["response_metadata"]
    if (not isinstance(metadata, dict)
            or set(metadata) != {"prompt_tokens", "completion_tokens", "finish_reason"}
            or any(not isinstance(metadata.get(key), int) or isinstance(metadata.get(key), bool)
                or metadata[key] <= 0 for key in ("prompt_tokens", "completion_tokens"))
            or not isinstance(metadata.get("finish_reason"), str) or not metadata["finish_reason"]):
        return {"pass": False, "runtime_contract_pass": False,
            "code": "response_usage_missing_or_inconsistent"}
    if (metadata["prompt_tokens"] != payload["authoritative_prompt_tokens"]
            or local["prompt_tokens"] != payload["authoritative_prompt_tokens"]):
        return {"pass": False, "runtime_contract_pass": False,
            "code": "response_usage_missing_or_inconsistent"}
    if (not isinstance(payload["request_duration_s"], (int, float))
            or isinstance(payload["request_duration_s"], bool)
            or not math.isfinite(payload["request_duration_s"])
            or not 0 <= payload["request_duration_s"] <= timeout_s):
        return {"pass": False, "runtime_contract_pass": False,
            "code": "local_timing_record_malformed"}
    if payload["atomic_response_completed"] is not True or payload["post_terminal_observations"]:
        return {"pass": False, "runtime_contract_pass": False,
            "code": "encrypted_progress_delivery_invalid"}
    semantic = evaluate_semantic(payload.get("response_text", ""), manifest)
    metrics = summarize_metrics(start_s=0.0, preparing_end_s=local["preparing_end_s"],
        prefill_end_s=local["prefill_end_s"], first_token_s=local["first_token_s"],
        inference_duration_s=local["inference_duration_s"],
        request_duration_s=payload["request_duration_s"],
        prompt_tokens=payload["authoritative_prompt_tokens"],
        output_tokens=metadata["completion_tokens"], request_budget_s=timeout_s)
    authoritative_local_progress = {"pass": True,
        "progress_event_count": len(local["events"]), "observed_phases": local["phases"]}
    response_usage = {**metadata, "source": "validated_atomic_response_usage"}
    evidence = {
        "runner_kind": "repository_packaged_desktop_webdriver",
        "fixture": {"id": fixture_id, "sha256": manifest.get("fixture_sha256"),
            "requested_prompt_tokens": manifest.get("requested_tokens"),
            "estimated_prompt_tokens": manifest.get("actual_tokens"),
            "estimated_tokenizer": manifest.get("token_count_provenance"),
            "authoritative_prompt_tokens": payload.get("authoritative_prompt_tokens"),
            "authoritative_target_offsets_tokens": authoritative_offsets,
            "authoritative_target_ratios": {key: value / payload["authoritative_prompt_tokens"]
                for key, value in authoritative_offsets.items()}},
        "semantic": semantic,
        "generation_settings": generation_settings,
        "response_usage": response_usage,
        "authoritative_local_progress": authoritative_local_progress,
        "encrypted_progress": progress,
        "atomic_response_completion": {"completed": True,
            "source": "browser_decrypted_final_response"},
        "post_terminal_silence": {"observed": True,
            "source": "pre_cancellation_primary_snapshot"},
        "metrics": metrics,
        "memory": memory,
        "runtime": {key: payload[key] for key in ("app_identity", "runtime_identity",
            "bundled_runtime_identity", "build_identity", "backend_requested", "backend_selected",
            "backend_used", "model_fingerprint",
            "authoritative_prompt_tokens")},
    }
    if cancellation_validation:
        try:
            evidence["cancellation_recovery"] = validate_cancellation_recovery(
                payload.get("cancellation_recovery"), cleanup_budget_s=float(cleanup_timeout_s),
                observation_window_s=observation_window_s, recovery_timeout_s=recovery_timeout_s,
                total_prompt_tokens=payload["authoritative_prompt_tokens"],
                prefill_threshold=prefill_cancel_tokens or max(1, int(
                    payload["authoritative_prompt_tokens"] * float(prefill_cancel_fraction))),
                generation_threshold=generation_cancel_tokens)
        except ValueError as exc:
            return {"pass": False, "runtime_contract_pass": False, "code": str(exc)}
    try:
        applicability = validate_kv_applicability(payload.get("kv_applicability"),
            backend=backend, context_tier=context_tier)
    except ValueError as exc:
        return {"pass": False, "runtime_contract_pass": False, "code": str(exc)}
    p7_required = applicability["applicability"] == "qwen_64k_full"
    if p7_required:
        evidence["kv_compare"] = compare_kv_estimate(payload.get("kv_estimate"), payload.get("kv_runtime"),
            backend=backend, context_tokens=65536)
        if (evidence["kv_compare"].get("pass")
                and evidence["kv_compare"].get("profile_id") != applicability["profile_id"]):
            evidence["kv_compare"] = {"pass": False, "code": "kv_diagnostic_provenance_mismatch"}
        elif evidence["kv_compare"].get("pass"):
            evidence["kv_compare"]["attestation"] = applicability
    else:
        reason = applicability["applicability"]
        evidence["kv_compare"] = {"pass": True, "applicability": reason, "reason": reason,
            "attestation": applicability}
    try:
        evidence["runtime_configuration"] = validate_runtime_configuration(
            payload["runtime_configuration"], backend=backend, context_tier=context_tier,
            context_tokens=profile.total_context_tokens, kv_attestation=evidence["kv_compare"])
    except ValueError as exc:
        return {"pass": False, "runtime_contract_pass": False, "code": str(exc)}
    runtime = evidence["runtime"]
    identity_ok = (runtime["app_identity"] not in {"dev", "mock", "unknown"} and
        runtime["runtime_identity"] == runtime.get("bundled_runtime_identity") and
        runtime["backend_requested"] == backend and runtime["backend_selected"] == backend and
        runtime["backend_used"] == backend)
    runtime_contract_pass = bool(progress.get("pass") and metrics.get("pass") and identity_ok
        and payload["authoritative_prompt_tokens"] == local["prompt_tokens"])
    if cancellation_validation:
        runtime_contract_pass = runtime_contract_pass and bool(
            evidence.get("cancellation_recovery", {}).get("pass"))
    if p7_required:
        runtime_contract_pass = runtime_contract_pass and bool(evidence["kv_compare"].get("pass"))
    passed = bool(runtime_contract_pass and semantic.get("semantic_pass"))
    evidence["runtime_contract_pass"] = runtime_contract_pass
    evidence["report_only_accepted"] = bool(report_only and runtime_contract_pass and not semantic.get("semantic_pass"))
    evidence["pass"] = passed
    evidence["code"] = "ok" if passed else "packaged_contract_failed"
    return sanitize(evidence)

MATRIX_PLAN_SCHEMA_VERSION = "long-context-benchmark-matrix-plan-v1"
MATRIX_PACKAGED_BACKENDS = (
    ("linux", "cpu", "linux-packaged-cpu"),
    ("macos", "cpu", "macos-packaged-cpu"),
    ("macos", "metal", "macos-packaged-metal"),
    ("windows", "cpu", "windows-packaged-cpu"),
    ("windows", "cuda", "windows-nvidia-packaged-cuda"),
)
MATRIX_WORKLOADS = (
    ("8k-fast", "small-8k"),
    ("64k-full", "intermediate-32k"),
    ("64k-full", "long-55k"),
)


def build_matrix_plan() -> dict[str, Any]:
    cells = []
    for platform_name, backend, package in MATRIX_PACKAGED_BACKENDS:
        for context_tier, fixture in MATRIX_WORKLOADS:
            for scenario in ("single-needle", "structured-extraction"):
                cells.append({"platform": platform_name, "package": package, "backend": backend,
                    "context_tier": context_tier, "fixture": fixture, "scenario": scenario,
                    "trials": 3, "cancellation_sequences": 0})
        cells.append({"platform": platform_name, "package": package, "backend": backend,
            "context_tier": "64k-full", "fixture": "long-55k",
            "scenario": "structured-extraction", "trials": 0, "cancellation_sequences": 1})
    return {"schema_version": MATRIX_PLAN_SCHEMA_VERSION, "cells": cells}


def validate_matrix_plan(plan: Any) -> None:
    if not isinstance(plan, dict) or set(plan) != {"schema_version", "cells"} \
            or plan["schema_version"] != MATRIX_PLAN_SCHEMA_VERSION or not isinstance(plan["cells"], list):
        raise ValueError("matrix_plan_invalid")
    expected = build_matrix_plan()["cells"]
    if plan["cells"] != expected or len({_canonical_json(cell) for cell in plan["cells"]}) != len(expected):
        raise ValueError("matrix_plan_invalid")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Long-context packaged-runtime benchmark harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("matrix-plan", help="emit the deterministic packaged benchmark execution matrix")
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
    r.add_argument("--trials", type=int, default=1,
        help=f"number of sequential physical trials (1-{MAX_PACKAGED_TRIALS}; default: 1)")
    r.add_argument("--cancellation-validation", action="store_true",
        help="run one physical prefill/generation cancellation and recovery sequence")
    prefill = r.add_mutually_exclusive_group()
    prefill.add_argument("--prefill-cancel-tokens", type=int)
    prefill.add_argument("--prefill-cancel-fraction", type=float)
    r.add_argument("--generation-cancel-tokens", type=int, default=1)
    r.add_argument("--cancellation-observation-window", type=float, default=0.5)
    r.add_argument("--cancellation-recovery-timeout", type=float, default=30.0)
    args = p.parse_args(argv)
    if args.cmd == "matrix-plan":
        plan = build_matrix_plan(); validate_matrix_plan(plan); print(_canonical_json(plan)); return 0
    if args.cmd == "generate-fixture":
        prompt, manifest = generate_fixture(args.fixture, args.seed, scenario=args.scenario); validate_manifest(manifest, prompt); out=Path(args.out_dir); out.mkdir(parents=True, exist_ok=True); (out/f"{args.fixture}.prompt.txt").write_text(prompt); (out/f"{args.fixture}.manifest.json").write_text(_canonical_json(manifest)+"\n"); print(f"generated {args.fixture}: scenario={args.scenario} requested={manifest['requested_tokens']} actual={manifest['actual_tokens']} sha256={manifest['fixture_sha256']}"); return 0
    if args.cmd == "evaluate":
        manifest=json.loads(Path(args.manifest).read_text()); validate_manifest(manifest); response=Path(args.response).read_text(); score=evaluate_semantic(response, manifest); path=write_report_atomic(Path(args.out_dir), {"mode":"semantic-evaluation", "status":"passed" if score["semantic_pass"] else "failed", "semantic":score,"fixture":{"id":manifest["fixture_id"], "version":manifest["fixture_version"], "scenario":manifest["scenario"], "sha256":manifest["fixture_sha256"]}}); print(f"semantic_pass={score.get('semantic_pass', False)} report={path}"); return 1 if args.strict and not score.get("semantic_pass") else 0
    if args.cmd == "packaged-runtime":
        if isinstance(args.trials, bool) or not 1 <= args.trials <= MAX_PACKAGED_TRIALS:
            p.error(f"--trials must be between 1 and {MAX_PACKAGED_TRIALS}")
        if bool(args.prompt) != bool(args.manifest):
            p.error("--prompt and --manifest are mutually required")
        if args.cancellation_validation and (
                (args.prefill_cancel_tokens is None) == (args.prefill_cancel_fraction is None)
                or args.prefill_cancel_tokens is not None and args.prefill_cancel_tokens <= 0
                or args.prefill_cancel_fraction is not None and not 0 < args.prefill_cancel_fraction < 1
                or not 1 <= args.generation_cancel_tokens <= 65536
                or not 0 < args.cancellation_observation_window <= 300
                or not 0 < args.cancellation_recovery_timeout <= 300):
            p.error("cancellation validation requires one bounded prefill trigger and bounded timeouts")
        external_prompt = _read_bounded_text(args.prompt) if args.prompt else None
        try:
            external_manifest = (json.loads(_read_bounded_text(args.manifest, 1024 * 1024))
                if args.manifest else None)
        except json.JSONDecodeError:
            # Preserve the adapter's stable categorical fail-closed report path.
            external_manifest = []
        if external_manifest is None:
            _validated_prompt, validated_manifest = generate_fixture(args.fixture, scenario=args.scenario)
        else:
            validated_manifest = external_manifest
        validated_fixture_sha256 = "unavailable"
        try:
            validate_manifest(validated_manifest, external_prompt)
        except (TypeError, ValueError):
            pass
        else:
            if (validated_manifest["fixture_id"] == args.fixture
                    and validated_manifest["scenario"] == args.scenario):
                validated_fixture_sha256 = validated_manifest["fixture_sha256"]
        completed: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {"pass": False, "code": "packaged_contract_failed"}
        settings: dict[str, Any] | None = None
        runtime_configuration: dict[str, Any] | None = None
        cancellation_evidence = None
        for trial_index in range(args.trials):
            evidence = invoke_packaged_runtime_adapter(fixture_id=args.fixture, scenario=args.scenario, timeout_s=args.request_timeout,
                app_binary=args.app_binary, model=args.model, backend=args.backend, relay_url=args.relay_url,
                cleanup_timeout_s=args.cleanup_timeout, context_tier=args.context_tier,
                report_only=args.report_only, external_prompt=external_prompt,
                external_manifest=external_manifest,
                cancellation_validation=args.cancellation_validation and trial_index == 0,
                prefill_cancel_tokens=args.prefill_cancel_tokens,
                prefill_cancel_fraction=args.prefill_cancel_fraction,
                generation_cancel_tokens=args.generation_cancel_tokens,
                observation_window_s=args.cancellation_observation_window,
                recovery_timeout_s=args.cancellation_recovery_timeout)
            if not evidence.get("runtime_contract_pass"):
                break
            if trial_index == 0 and args.cancellation_validation:
                cancellation_evidence = evidence.get("cancellation_recovery")
            if settings is None:
                settings = evidence.get("generation_settings")
            elif evidence.get("generation_settings") != settings:
                evidence = {"pass": False, "runtime_contract_pass": False,
                    "code": "generation_settings_inconsistent"}
                break
            if runtime_configuration is None:
                runtime_configuration = evidence.get("runtime_configuration")
            elif evidence.get("runtime_configuration") != runtime_configuration:
                evidence = {"pass": False, "runtime_contract_pass": False,
                    "code": "runtime_configuration_drift"}
                break
            completed.append(evidence)
        all_runtime_complete = len(completed) == args.trials
        if all_runtime_complete:
            aggregate = _aggregate_trial_scores([trial["semantic"] for trial in completed])
            all_semantic_pass = aggregate["exact_match_count"] == args.trials
            evidence = completed[-1]
            profile = get_context_profile(args.context_tier)
            report_only_accepted = bool(args.report_only and not all_semantic_pass)
            report = {"mode":"packaged-runtime", "status":"passed" if all_semantic_pass else "failed",
                "code":"ok" if all_semantic_pass else "semantic_failure", "overall_pass":all_semantic_pass,
                "report_only_accepted":report_only_accepted, "requested_trial_count":args.trials,
                "completed_trial_count":len(completed), "generation_settings":settings, "fixture":{
                "id":args.fixture, "version":FIXTURE_VERSION, "scenario":args.scenario,
                "sha256":evidence["fixture"]["sha256"]}, "runtime":evidence["runtime"],
                "backend":{"requested":evidence["runtime"]["backend_requested"],
                    "selected":evidence["runtime"]["backend_selected"], "used":evidence["runtime"]["backend_used"]},
                "context":{"tier":args.context_tier, "window_tokens":profile.total_context_tokens,
                    "output_reservation_tokens":profile.default_output_reservation_tokens,
                    "prompt_tokens":evidence["fixture"]["authoritative_prompt_tokens"],
                    "output_tokens":evidence["metrics"]["output_tokens"]},
                "authoritative_local_progress":evidence["authoritative_local_progress"],
                "encrypted_progress":evidence["encrypted_progress"],
                "response_usage":evidence["response_usage"],
                "atomic_response_completion":evidence["atomic_response_completion"],
                "post_terminal_silence":evidence["post_terminal_silence"],
                "metrics":evidence["metrics"],
                "semantic":evidence["semantic"], "aggregate_semantic":aggregate,
                "memory":{"maximum_peak_rss_bytes":max(
                    trial["memory"]["peak_rss_bytes"] for trial in completed),
                    "trials":[trial["memory"] for trial in completed]},
                "runtime_configuration":{"trials":[
                    trial["runtime_configuration"] for trial in completed]},
                "kv_diagnostics":{"trials":[trial["kv_compare"] for trial in completed]}}
            if args.cancellation_validation:
                report["cancellation_recovery"] = cancellation_evidence
        else:
            report = {"mode":"packaged-runtime", "status":"not_run", "code":evidence.get("code", "packaged_contract_failed"),
                "requested_trial_count":args.trials, "completed_trial_count":len(completed),
                "fixture":{"id":args.fixture, "version":FIXTURE_VERSION, "scenario":args.scenario,
                    "sha256":validated_fixture_sha256}}
            for key in ("last_safe_phase", "failure_reason", "request_timeout_s", "setup_timeout_s",
                    "finalization_timeout_s", "cancellation_timeout_s", "cleanup_timeout_s", "runner_timeout_s",
                    "overall_timeout_s", "elapsed_s", "cleanup_succeeded"):
                if key in evidence:
                    report[key] = evidence[key]
        path=write_report_atomic(Path(args.out_dir), report)
        print(f"packaged_runtime_pass={evidence.get('pass', False)} report={path}")
        return 0 if all_runtime_complete and (all_semantic_pass or report_only_accepted) else 1
    return 2
if __name__ == "__main__": raise SystemExit(main())
