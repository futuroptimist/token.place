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
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from utils.context_profiles import get_context_profile

SCHEMA_VERSION = "p8-benchmark-report-v1"
FIXTURE_VERSION = "p8-semantic-haystack-v1"
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

def generate_fixture(fixture_id: str, seed: str = DEFAULT_SEED, tokenizer: Callable[[str], int] | None = None) -> tuple[str, dict[str, Any]]:
    spec = FIXTURES[fixture_id]
    canary = "lunar-maple-508163"
    targets = {
        "VII": "They were obliged to camp",
        "XIV": "You will remember there was",
        "XXI": "After climbing down from the",
        "canary": canary,
        "needle": f"{seed}-{fixture_id}-needle",
    }
    toc = "\n".join(["Table of Contents", "VII. They were obliged to camp out", "XIV. The Winged Monkeys", "XXI. The Lion Becomes the King"])
    prompt_parts = [
        "You must answer with JSON only, no Markdown, no commentary, and exactly the keys VII, XIV, XXI, canary.",
        "For VII, XIV, and XXI return exactly the first five whitespace-separated words of the first prose sentence in that chapter, preserving capitalization and omitting trailing punctuation.",
        f"For canary return exactly {canary}.",
        toc,
    ]
    chapter_sentences = {
        "VII": "They were obliged to camp beside the road before sunrise. This prose sentence is not the heading.",
        "XIV": "You will remember there was no road--not even a pathway--between the castle and the city. This prose sentence is not the title.",
        "XXI": "After climbing down from the China wall the travelers found themselves in a disagreeable country. This prose sentence is not the title.",
    }
    target_markers: dict[str, int] = {}
    positions = {"VII": 0.18, "XIV": 0.52, "XXI": 0.84}
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
                target_markers[chap] = cur
                prompt_parts.append(f"\nChapter {chap}: {toc.splitlines()[['VII','XIV','XXI'].index(chap)+1]}\n{chapter_sentences[chap]}")
                inserted = True
        if not inserted:
            prompt_parts.append(f"Decoy paragraph {filler_i:05d} repeats chapter-title-like text but contains no answer. The phrase {targets['needle']} is decorative, not requested.")
            filler_i += 1
    for chap in ("VII", "XIV", "XXI"):
        if chap not in target_markers:
            target_markers[chap] = _count_tokens("\n".join(prompt_parts), tokenizer)
            prompt_parts.append(f"\nChapter {chap}: decoy heading\n{chapter_sentences[chap]}")
    prompt_parts.append(f"Final exact canary line: {canary}")
    prompt = "\n".join(prompt_parts).rstrip() + "\n"
    actual = _count_tokens(prompt, tokenizer)
    manifest = {
        "fixture_version": FIXTURE_VERSION, "fixture_id": fixture_id, "seed": seed,
        "requested_tokens": spec.requested_tokens, "actual_tokens": actual, "tokenizer": "adapter" if tokenizer else "whitespace-ci",
        "fixture_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "target_depths_tokens": target_markers,
        "expected_answers": {"VII": targets["VII"], "XIV": targets["XIV"], "XXI": targets["XXI"], "canary": canary},
        "semantic_oracle": {
            "prose_keys": ["VII", "XIV", "XXI"],
            "heading_decoys": ["They were obliged to camp out", "The Winged Monkeys", "The Lion Becomes the King"],
        },
        "scoring_rules": ["json_only", "exact_key_set", "canary_exact", "target_selection", "prose_not_heading", "word_count", "capitalization", "trailing_punctuation", "exact_match", "semantic_pass"],
    }
    return prompt, manifest

def evaluate_semantic(response_text: str, manifest: dict[str, Any]) -> dict[str, Any]:
    expected = manifest["expected_answers"]
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

def analyze_progress(events: Iterable[dict[str, Any]], terminal: str | None = None) -> dict[str, Any]:
    last_seq = -1; last_processed = 0; last_generated = 0; last_elapsed = 0; total = None; last_phase = -1; errors=[]; count=0; first=None; final=None
    for ev in events:
        count += 1; first = first or ev; final = ev
        phase = ev.get("phase")
        if phase not in PHASES: errors.append("invalid_phase"); continue
        if PHASES[phase] < last_phase: errors.append("invalid_phase_transition")
        last_phase = max(last_phase, PHASES[phase])
        seq = ev.get("sequence")
        if not isinstance(seq, int) or seq <= last_seq: errors.append("decreasing_sequence")
        last_seq = seq if isinstance(seq, int) else last_seq
        p = ev.get("processed_prompt_tokens"); c = ev.get("cached_prompt_tokens"); g = ev.get("generated_tokens"); t = ev.get("total_prompt_tokens"); elapsed = ev.get("elapsed_ms")
        if not all(isinstance(x, int) and x >= 0 for x in (p,c,g,t,elapsed)): errors.append("malformed_telemetry"); continue
        if total is None: total = t
        elif total != t: errors.append("changing_prompt_total")
        if p < last_processed: errors.append("decreasing_processed")
        if g < last_generated: errors.append("decreasing_generated")
        if elapsed < last_elapsed: errors.append("decreasing_elapsed")
        if c > p: errors.append("cached_exceeds_processed")
        if p > t: errors.append("processed_exceeds_total")
        last_processed, last_generated, last_elapsed = p, g, elapsed
    if terminal not in {None, "completed", "cancelled", "error"}: errors.append("invalid_terminal_state")
    if terminal == "completed" and final and final.get("phase") != "generating": errors.append("terminal_lifecycle_without_generation")
    if terminal == "cancelled" and not final: errors.append("terminal_lifecycle_without_progress")
    return {"pass": not errors, "errors": errors, "progress_event_count": count, "first_progress": first, "final_progress": final}

def summarize_metrics(start: float, first_token: float | None, end: float, prompt_tokens: int, output_tokens: int) -> dict[str, Any]:
    total = max(end-start, 0.0); prefill = (first_token-start) if first_token is not None else None; decode = (end-first_token) if first_token is not None else None
    return {"total_duration_s": total, "prefill_duration_s": prefill, "decode_duration_s": decode, "prompt_tokens_per_s": (prompt_tokens/prefill if prefill and prefill>0 else None), "decode_tokens_per_s": (output_tokens/decode if decode and decode>0 else None)}

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

def write_report_atomic(out_dir: Path, report: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = sanitize({"schema_version": SCHEMA_VERSION, **report})
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


def _run_owned_runner(command: list[str], timeout_s: float,
        cleanup_timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run one owned process group without buffering output or killing by name."""
    kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)  # noqa: S603
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
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=cleanup_timeout_s, check=False)  # noqa: S603
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=cleanup_timeout_s)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=cleanup_timeout_s)
        raise
    drain.join(timeout=cleanup_timeout_s)
    tail = b"".join(chunks)[-2048:].decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(command, returncode, stdout=tail)


def invoke_packaged_runtime_adapter(*, fixture_id: str = "small-8k", timeout_s: float = 30.0,
        model: str | None = None, backend: str | None = None, relay_url: str | None = None,
        cleanup_timeout_s: float | None = None, app_binary: str | None = None,
        context_tier: str = "64k-full", report_only: bool = False,
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
    prompt, manifest = generate_fixture(fixture_id)
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
        "backend_used", "model_fingerprint", "authoritative_prompt_tokens", "progress_events",
        "terminal", "response_text", "start_s", "first_token_s", "end_s", "output_tokens"}
    missing_evidence = sorted(key for key in required if payload.get(key) in (None, "", [], {}))
    if missing_evidence:
        return {"pass": False, "code": "packaged_evidence_missing", "missing": missing_evidence}
    progress = analyze_progress(payload.get("progress_events", []), payload.get("terminal", "completed"))
    semantic = evaluate_semantic(payload.get("response_text", ""), manifest)
    metrics = summarize_metrics(
        float(payload.get("start_s", 0.0)),
        payload.get("first_token_s"),
        float(payload.get("end_s", 0.0)),
        int(manifest.get("actual_tokens", 0)),
        int(payload.get("output_tokens", 0)),
    )
    evidence = {
        "runner_kind": "repository_packaged_desktop_webdriver",
        "fixture": {"id": fixture_id, "sha256": manifest.get("fixture_sha256"), "actual_tokens": manifest.get("actual_tokens")},
        "semantic": semantic,
        "progress": progress,
        "metrics": metrics,
        "memory": payload.get("memory", {}),
        "runtime": {key: payload[key] for key in ("app_identity", "runtime_identity",
            "bundled_runtime_identity", "build_identity", "backend_used", "model_fingerprint",
            "authoritative_prompt_tokens")},
    }
    if "kv_estimate" in payload or "kv_runtime" in payload:
        evidence["kv_compare"] = compare_kv_estimate(payload.get("kv_estimate", {}), payload.get("kv_runtime", {}))
    runtime = evidence["runtime"]
    identity_ok = (runtime["app_identity"] not in {"dev", "mock", "unknown"} and
        runtime["runtime_identity"] == runtime.get("bundled_runtime_identity") and
        runtime["backend_used"] == backend)
    runtime_contract_pass = bool(progress.get("pass") and identity_ok)
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
    progress = analyze_progress(events, "cancelled")
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
    g = sub.add_parser("generate-fixture"); g.add_argument("--fixture", choices=FIXTURES, required=True); g.add_argument("--out-dir", required=True); g.add_argument("--seed", default=DEFAULT_SEED)
    e = sub.add_parser("evaluate"); e.add_argument("--manifest", required=True); e.add_argument("--response", required=True); e.add_argument("--strict", action="store_true"); e.add_argument("--out-dir", required=True)
    r = sub.add_parser("packaged-runtime", help="run the installed desktop through repository WebDriver control")
    r.add_argument("--out-dir", required=True); r.add_argument("--fixture", choices=FIXTURES, default="small-8k")
    r.add_argument("--app-binary", required=True); r.add_argument("--model", required=True)
    r.add_argument("--backend", choices=("metal", "cuda", "cpu"), required=True); r.add_argument("--relay-url", required=True)
    r.add_argument("--context-tier", choices=("8k-fast", "64k-full"), default="64k-full")
    r.add_argument("--request-timeout", type=float, default=600.0); r.add_argument("--cleanup-timeout", type=float, default=30.0)
    r.add_argument("--report-only", action="store_true", help="preserve semantic failures; runtime failures remain nonzero")
    args = p.parse_args(argv)
    if args.cmd == "generate-fixture":
        prompt, manifest = generate_fixture(args.fixture, args.seed); out=Path(args.out_dir); out.mkdir(parents=True, exist_ok=True); (out/f"{args.fixture}.prompt.txt").write_text(prompt); (out/f"{args.fixture}.manifest.json").write_text(_canonical_json(manifest)+"\n"); print(f"generated {args.fixture}: requested={manifest['requested_tokens']} actual={manifest['actual_tokens']} sha256={manifest['fixture_sha256']}"); return 0
    if args.cmd == "evaluate":
        manifest=json.loads(Path(args.manifest).read_text()); response=Path(args.response).read_text(); score=evaluate_semantic(response, manifest); path=write_report_atomic(Path(args.out_dir), {"mode":"semantic-evaluation","semantic":score,"fixture":{"id":manifest.get("fixture_id"),"sha256":manifest.get("fixture_sha256"),"actual_tokens":manifest.get("actual_tokens")}}); print(f"semantic_pass={score.get('semantic_pass', False)} report={path}"); return 1 if args.strict and not score.get("semantic_pass") else 0
    if args.cmd == "packaged-runtime":
        evidence = invoke_packaged_runtime_adapter(fixture_id=args.fixture, timeout_s=args.request_timeout,
            app_binary=args.app_binary, model=args.model, backend=args.backend, relay_url=args.relay_url,
            cleanup_timeout_s=args.cleanup_timeout, context_tier=args.context_tier,
            report_only=args.report_only)
        path=write_report_atomic(Path(args.out_dir), {"mode":"packaged-runtime",
            "runtime":{"platform":platform.system().lower()},"evidence":evidence})
        print(f"packaged_runtime_pass={evidence.get('pass', False)} report={path}")
        return 0 if evidence.get("pass") or evidence.get("report_only_accepted") else 1
    return 2
if __name__ == "__main__": raise SystemExit(main())
