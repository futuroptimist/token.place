"""P8 packaged-runtime benchmark harness utilities.

The module is intentionally adapter-first: unit tests use canned adapters, while
``--mode packaged`` validates prerequisites and fails closed unless it can use the
real packaged desktop compute-node/API v1 runtime.
"""
from __future__ import annotations

import argparse, hashlib, json, os, platform, re, subprocess, sys, tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from utils.llm.model_manager import _qwen_64k_memory_estimate

SCHEMA_VERSION = "p8-runtime-benchmark-report/v1"
FIXTURE_VERSION = "p8-semantic-haystack/v1"
DEFAULT_SEED = 1566
TIERS = {"8k": 8192, "32k": 32768, "55k": 55254}
TARGET_RATIOS = {"early": 0.12, "middle": 0.52, "late": 0.87}
EXPECTED_KEYS = ("VII", "XIV", "XXI", "canary")
SECRET_PATTERNS = [re.compile(p, re.I) for p in [r"/Users/[^\s,;]+", r"[A-Z]:\\[^\s,;]+", r"-----BEGIN [^-]+-----", r"ciphertext", r"\biv\b", r"cancel(?:lation)?[_-]?token", r"request[_-]?id", r"session[_-]?id"]]


def count_tokens_approx(text: str) -> int:
    # Deterministic CI tokenizer surrogate. Packaged mode can supply an adapter.
    return len(text.split())


def _filler(i: int) -> str:
    return f"archive filler line {i:05d} repeats chapter VII XIV XXI headings while hiding no answer."


def generate_fixture(tier: str, seed: int = DEFAULT_SEED, tokenizer: Callable[[str], int] | None = None) -> dict[str, Any]:
    if tier not in TIERS:
        raise ValueError(f"unsupported_fixture_tier:{tier}")
    target_tokens = TIERS[tier]
    tok = tokenizer or count_tokens_approx
    canary = f"lunar-maple-{(seed * 31337) % 1000000:06d}"
    targets = {
        "VII": "They were obliged to camp",
        "XIV": "You will remember there was",
        "XXI": "After climbing down from the",
        "canary": canary,
    }
    sections = [
        "P8 synthetic haystack. Return JSON only with keys VII, XIV, XXI, canary. Preserve exact capitalization and omit trailing punctuation for chapter answers.",
        "Table of Contents: VII They were obliged to camp out; XIV The Winged Monkeys; XXI The Lion Becomes the King.",
        "Decoy: They were obliged to camp out. Decoy: The Winged Monkeys. Decoy: The Lion Becomes the King.",
    ]
    placements: dict[str, int] = {}
    items = list(TARGET_RATIOS.items())
    target_by_depth = {"early": ("VII", targets["VII"] + " when the clouds closed around them."), "middle": ("XIV", targets["XIV"] + " a golden cap hidden in plain sight."), "late": ("XXI", targets["XXI"] + " emerald steps they rested without speaking.")}
    idx = 0
    for depth, ratio in items:
        while tok("\n".join(sections)) < int(target_tokens * ratio):
            sections.append(_filler(idx)); idx += 1
        key, sentence = target_by_depth[depth]
        placements[key] = tok("\n".join(sections))
        sections.append(f"Chapter {key}: { {'VII':'They were obliged to camp out','XIV':'The Winged Monkeys','XXI':'The Lion Becomes the King'}[key] }")
        sections.append(f"First prose sentence for {key}: {sentence}")
    sections.append(f"Exact canary line: {canary}")
    while tok("\n".join(sections)) < target_tokens:
        sections.append(_filler(idx)); idx += 1
    prompt = "\n".join(sections)
    actual = tok(prompt)
    fixture_hash = hashlib.sha256(prompt.encode()).hexdigest()
    manifest = {"fixture_version": FIXTURE_VERSION, "seed": seed, "fixture_id": f"synthetic-{tier}", "fixture_hash_sha256": fixture_hash, "requested_tokens": target_tokens, "actual_tokens": actual, "target_depths_tokens": placements, "expected_answers": targets, "expected_keys": list(EXPECTED_KEYS), "scoring_rules": {"word_counts": {k: len(v.split()) for k,v in targets.items()}, "json_only": True, "trailing_punctuation_forbidden_keys": ["VII","XIV","XXI"]}}
    return {"prompt": prompt, "manifest": manifest}


def evaluate_semantic(response: str, manifest: dict[str, Any]) -> dict[str, Any]:
    stripped = response.strip()
    failures: list[str] = []
    json_only = stripped.startswith("{") and stripped.endswith("}") and "```" not in stripped and response == stripped
    if not json_only: failures.append("json_not_only")
    try:
        obj = json.loads(stripped)
        valid_json = isinstance(obj, dict)
    except Exception:
        obj, valid_json = {}, False; failures.append("invalid_json")
    exp = manifest["expected_answers"]; expected_keys = set(manifest["expected_keys"])
    exact_keys = valid_json and set(obj.keys()) == expected_keys
    if not exact_keys: failures.append("key_set_mismatch")
    categories = {"valid_json_no_markdown_or_commentary": valid_json and json_only, "exact_key_set": exact_keys, "exact_canary": obj.get("canary") == exp.get("canary"), "correct_target_selection": True, "prose_vs_heading_selection": True, "exact_whitespace_word_count": True, "capitalization_preserved": True, "trailing_punctuation_rules": True}
    headings = {"XIV":"The Winged Monkeys", "XXI":"The Lion Becomes the King", "VII":"They were obliged to camp out"}
    for k,v in exp.items():
        got = obj.get(k)
        if got != v:
            if k == "canary": failures.append("canary_mismatch")
            else: failures.append(f"exact_mismatch_{k}"); categories["correct_target_selection"] = False
        if k in headings and got == headings[k]: categories["prose_vs_heading_selection"] = False; failures.append(f"heading_substitution_{k}")
        if isinstance(got, str) and len(got.split()) != len(v.split()): categories["exact_whitespace_word_count"] = False; failures.append(f"word_count_{k}")
        if isinstance(got, str) and got.lower() == v.lower() and got != v: categories["capitalization_preserved"] = False; failures.append(f"capitalization_{k}")
        if k in {"VII","XIV","XXI"} and isinstance(got,str) and got.endswith(('.',',',';','!','?')): categories["trailing_punctuation_rules"] = False; failures.append(f"punctuation_{k}")
    categories["complete_exact_match"] = valid_json and exact_keys and all(obj.get(k)==v for k,v in exp.items()) and all(categories.values())
    return {"semantic_pass": categories["complete_exact_match"], "categories": categories, "failure_codes": sorted(set(failures))}


def score_trials(responses: list[str], manifest: dict[str, Any]) -> dict[str, Any]:
    results = [evaluate_semantic(r, manifest) for r in responses]
    return {"trial_count": len(results), "exact_match_count": sum(1 for r in results if r["semantic_pass"]), "pass_rate": (sum(1 for r in results if r["semantic_pass"]) / len(results)) if results else 0, "trials": results}

PHASE_ORDER = {"preparing":0,"prefill":1,"generation":2,"complete":3,"cancelled":3,"error":3}
def validate_progress(events: list[dict[str, Any]]) -> dict[str, Any]:
    failures=[]; last_seq=-1; last_proc=-1; last_gen=-1; total=None; terminal=False; last_phase=-1
    for e in events:
        seq=e.get("seq"); phase=e.get("phase"); proc=e.get("processed_tokens"); gen=e.get("generated_tokens",0); tot=e.get("prompt_total_tokens")
        if terminal: failures.append("progress_after_terminal")
        if not isinstance(seq,int) or seq <= last_seq: failures.append("sequence_decreased")
        if phase not in PHASE_ORDER or PHASE_ORDER.get(phase, -1) < last_phase: failures.append("invalid_phase_transition")
        if total is None: total=tot
        elif tot != total: failures.append("prompt_total_changed")
        if not isinstance(proc,int) or proc < last_proc: failures.append("processed_decreased")
        if isinstance(proc,int) and isinstance(tot,int) and proc > tot: failures.append("processed_exceeds_total")
        if not isinstance(gen,int) or gen < last_gen: failures.append("generated_decreased")
        if phase in {"complete","cancelled","error"}: terminal=True
        last_seq=seq if isinstance(seq,int) else last_seq; last_proc=proc if isinstance(proc,int) else last_proc; last_gen=gen if isinstance(gen,int) else last_gen; last_phase=max(last_phase, PHASE_ORDER.get(phase,-1))
    return {"pass": not failures, "failure_codes": sorted(set(failures)), "event_count": len(events), "first": events[0] if events else None, "final": events[-1] if events else None}


def calculate_metrics(events: list[dict[str, Any]], request_budget_seconds: float, output_tokens: int) -> dict[str, Any]:
    if not events: raise ValueError("missing_progress_telemetry")
    by={e["phase"]: e for e in events if "phase" in e and "elapsed_seconds" in e}
    total=events[-1].get("elapsed_seconds")
    if total is None: raise ValueError("missing_total_duration")
    prefill = (by.get("generation", by.get("complete", events[-1])).get("elapsed_seconds", total) - by.get("preparing", events[0]).get("elapsed_seconds",0))
    decode = max(0.0, total - by.get("generation", events[-1]).get("elapsed_seconds", total))
    prompt_tokens=events[-1].get("processed_tokens")
    return {"total_duration_seconds": total, "prefill_duration_seconds": prefill, "decode_duration_seconds": decode, "time_to_first_generated_token_seconds": by.get("generation",{}).get("elapsed_seconds"), "prompt_tokens_per_second": (prompt_tokens/prefill) if prompt_tokens and prefill else None, "decode_tokens_per_second": (output_tokens/decode) if output_tokens and decode else None, "request_budget_seconds": request_budget_seconds, "remaining_completion_margin_seconds": request_budget_seconds-total}


def compare_kv_estimate(estimate: dict[str, Any], runtime_diag: dict[str, Any], *, tolerance_bytes: int = 16*1024*1024, exact_required: bool = True) -> dict[str, Any]:
    est=estimate.get("exact_kv_allocation_bytes"); obs=runtime_diag.get("kv_allocation_bytes")
    if exact_required and (estimate.get("conservative_fallback_used") or est is None): return {"pass":False,"failure_code":"exact_estimate_unavailable"}
    if obs is None or not isinstance(obs,int): return {"pass":False,"failure_code":"runtime_kv_diagnostic_missing"}
    delta=abs(int(est)-obs)
    return {"pass": delta <= tolerance_bytes, "estimated_bytes": est, "observed_bytes": obs, "delta_bytes": delta, "tolerance_bytes": tolerance_bytes, "alignment_rule":"runtime allocation must be within 16 MiB of P7 exact allocation to allow allocator/page reporting granularity"}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict): return {str(k): sanitize(v) for k,v in value.items() if not SECRET_PATTERNS[-2].search(str(k))}
    if isinstance(value, list): return [sanitize(v) for v in value[:200]]
    if isinstance(value, str):
        s=value[:500]
        for p in SECRET_PATTERNS: s=p.sub("[redacted]", s)
        return s
    return value


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe=sanitize(data)
    fd,tmp=tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(safe,f,sort_keys=True,indent=2); f.write("\n")
    os.replace(tmp,path)

def p7_memory_estimate(model_path: str, n_ctx: int, kv_precision: str, backend: str, batch_profile: str = "balanced") -> dict[str, Any]:
    """Return P7 estimator output without duplicating the estimator formulas."""
    return _qwen_64k_memory_estimate(model_path, n_ctx, kv_precision, backend, batch_profile)


class MemoryProbe:
    def collect(self, timeout_seconds: float = 2.0) -> dict[str, Any]:
        system=platform.system().lower()
        if system == "darwin": cmd=["vm_stat"]
        elif system == "windows": cmd=["cmd","/c","echo","memory_probe_unavailable"]
        else: return {"available":False,"platform":system,"reason":"unsupported_platform"}
        try:
            cp=subprocess.run(cmd,capture_output=True,text=True,timeout=timeout_seconds,check=False)
        except subprocess.TimeoutExpired: return {"available":False,"platform":system,"reason":"timeout"}
        except OSError: return {"available":False,"platform":system,"reason":"probe_unavailable"}
        return sanitize({"available": cp.returncode==0, "platform":system, "summary": cp.stdout[:300] or cp.stderr[:300]})

@dataclass
class FakeRuntimeAdapter:
    response: str
    progress_events: list[dict[str, Any]]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False
    def run(self, prompt: str, settings: dict[str, Any], cancel_on: dict[str, Any] | None=None) -> dict[str, Any]:
        _ = (prompt, settings)
        events=[]
        for e in self.progress_events:
            events.append(e)
            if cancel_on and e.get(cancel_on.get("field"), -1) >= cancel_on.get("value", 10**18):
                self.cancelled=True; events.append({**e,"seq":e.get("seq",0)+1,"phase":"cancelled"}); break
        return {"response": None if self.cancelled else self.response, "progress_events": events, "diagnostics": self.diagnostics, "cancelled": self.cancelled, "recovery": {"followup_success": True, "operator_stop_start_success": True, "cleanup_within_budget": True}}


def build_report(manifest: dict[str, Any], run: dict[str, Any], mode: str, strict: bool, request_budget_seconds: float=480.0) -> dict[str, Any]:
    progress=validate_progress(run["progress_events"])
    sem=evaluate_semantic(run.get("response") or "{}", manifest) if run.get("response") is not None else {"semantic_pass": False, "categories": {}, "failure_codes": ["cancelled_no_response"]}
    metrics=calculate_metrics(run["progress_events"], request_budget_seconds, output_tokens=len((run.get("response") or "").split()))
    return {"schema_version":SCHEMA_VERSION,"mode":mode,"strict":strict,"fixture":{"id":manifest["fixture_id"],"version":manifest["fixture_version"],"hash_sha256":manifest["fixture_hash_sha256"],"requested_tokens":manifest["requested_tokens"],"actual_tokens":manifest["actual_tokens"],"target_depths_tokens":manifest["target_depths_tokens"]},"runtime":sanitize(run.get("diagnostics",{})),"generation_settings":{"temperature":0,"seed_supported":False},"semantic":sem,"progress":progress,"metrics":metrics,"cancellation":sanitize(run.get("recovery",{})),"privacy":{"prompt_body_included":False,"response_body_included":False}}


def main(argv: list[str] | None=None) -> int:
    ap=argparse.ArgumentParser(description="P8 packaged-runtime benchmark harness")
    sub=ap.add_subparsers(dest="cmd", required=True)
    g=sub.add_parser("generate-fixture"); g.add_argument("--tier",choices=TIERS,required=True); g.add_argument("--output-dir",required=True); g.add_argument("--seed",type=int,default=DEFAULT_SEED)
    e=sub.add_parser("evaluate"); e.add_argument("--manifest",required=True); e.add_argument("--response",required=True); e.add_argument("--strict",action="store_true")
    r=sub.add_parser("run"); r.add_argument("--mode",choices=["packaged","fake"],required=True); r.add_argument("--tier",choices=TIERS,default="8k"); r.add_argument("--output-dir",required=True); r.add_argument("--strict",action="store_true"); r.add_argument("--packaged-app")
    args=ap.parse_args(argv)
    if args.cmd=="generate-fixture":
        fx=generate_fixture(args.tier,args.seed); out=Path(args.output_dir); (out/f"{fx['manifest']['fixture_id']}.txt").write_text(fx["prompt"],encoding="utf-8"); atomic_write_json(out/f"{fx['manifest']['fixture_id']}.manifest.json",fx["manifest"]); print(json.dumps({k:fx['manifest'][k] for k in ('fixture_id','requested_tokens','actual_tokens','target_depths_tokens')},sort_keys=True)); return 0
    if args.cmd=="evaluate":
        m=json.loads(Path(args.manifest).read_text()); resp=Path(args.response).read_text(); res=evaluate_semantic(resp,m); print(json.dumps(res,sort_keys=True)); return 0 if (res["semantic_pass"] or not args.strict) else 2
    if args.mode=="packaged":
        if not args.packaged_app or not Path(args.packaged_app).exists():
            print("packaged_runtime_prerequisite_missing", file=sys.stderr); return 3
        print("packaged_runtime_adapter_not_available_without_installed_app_bridge", file=sys.stderr); return 3
    fx=generate_fixture(args.tier); run=FakeRuntimeAdapter(json.dumps(fx['manifest']['expected_answers'],sort_keys=True), [{"seq":0,"phase":"preparing","processed_tokens":0,"generated_tokens":0,"prompt_total_tokens":fx['manifest']['actual_tokens'],"elapsed_seconds":0.1},{"seq":1,"phase":"prefill","processed_tokens":fx['manifest']['actual_tokens'],"generated_tokens":0,"prompt_total_tokens":fx['manifest']['actual_tokens'],"elapsed_seconds":1.1},{"seq":2,"phase":"generation","processed_tokens":fx['manifest']['actual_tokens'],"generated_tokens":1,"prompt_total_tokens":fx['manifest']['actual_tokens'],"elapsed_seconds":1.2},{"seq":3,"phase":"complete","processed_tokens":fx['manifest']['actual_tokens'],"generated_tokens":4,"prompt_total_tokens":fx['manifest']['actual_tokens'],"elapsed_seconds":1.4}], {"backend_selected":"fake"}).run(fx['prompt'],{})
    report=build_report(fx['manifest'],run,args.mode,args.strict); atomic_write_json(Path(args.output_dir)/"p8-runtime-benchmark-report.json", report); print(f"semantic_pass={report['semantic']['semantic_pass']} total_seconds={report['metrics']['total_duration_seconds']}"); return 0

if __name__ == "__main__": raise SystemExit(main())
