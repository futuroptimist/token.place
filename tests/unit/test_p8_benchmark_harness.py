import json
import subprocess
import sys

from scripts.p8 import benchmark_harness as h


def test_fixture_generation_stable_hash_and_depths():
    p1, m1 = h.generate_fixture("small-8k")
    p2, m2 = h.generate_fixture("small-8k")
    assert p1 == p2
    assert m1 == m2
    assert m1["fixture_sha256"] == h.hashlib.sha256(p1.encode()).hexdigest()
    assert set(m1["target_depths_tokens"]) == {"VII", "XIV", "XXI"}
    assert m1["target_depths_tokens"]["VII"] < m1["target_depths_tokens"]["XIV"] < m1["target_depths_tokens"]["XXI"]
    assert "The Winged Monkeys" in p1 and "Table of Contents" in p1


def test_fixture_generation_sizes_with_ci_tokenizer():
    for fixture in ("small-8k", "intermediate-32k", "long-55k"):
        _, manifest = h.generate_fixture(fixture)
        assert manifest["requested_tokens"] <= manifest["actual_tokens"] + 300
        assert manifest["actual_tokens"] > manifest["requested_tokens"] - 500


def test_authoritative_tokenizer_hook_used():
    _, manifest = h.generate_fixture("small-8k", tokenizer=lambda text: len(text.split()) + 7)
    assert manifest["tokenizer"] == "adapter"
    assert manifest["actual_tokens"] >= 8192


def test_semantic_exact_success():
    _, manifest = h.generate_fixture("small-8k")
    response = json.dumps(manifest["expected_answers"])
    score = h.evaluate_semantic(response, manifest)
    assert score["semantic_pass"] is True
    assert score["exact_match"] is True


def test_semantic_known_p7_failures_detected():
    _, manifest = h.generate_fixture("small-8k")
    response = json.dumps({"VII":"They were obliged to camp out","XIV":"The Winged Monkeys","XXI":"The Lion Becomes the King","canary":"lunar-maple-508163"})
    score = h.evaluate_semantic(response, manifest)
    assert score["json_only"] is True
    assert score["exact_key_set"] is True
    assert score["canary_exact"] is True
    assert score["word_count"] is False
    assert score["prose_not_heading"] is False
    assert score["semantic_pass"] is False


def test_semantic_json_key_canary_format_failures():
    _, manifest = h.generate_fixture("small-8k")
    assert h.evaluate_semantic("```json\n{}\n```", manifest)["json_only"] is False
    extra = dict(manifest["expected_answers"], extra="x")
    assert h.evaluate_semantic(json.dumps(extra), manifest)["exact_key_set"] is False
    wrong = dict(manifest["expected_answers"], canary="wrong")
    assert h.evaluate_semantic(json.dumps(wrong), manifest)["canary_exact"] is False
    punct = dict(manifest["expected_answers"], VII="They were obliged to camp.")
    assert h.evaluate_semantic(json.dumps(punct), manifest)["trailing_punctuation"] is False
    cap = dict(manifest["expected_answers"], XIV="you will remember there was")
    assert h.evaluate_semantic(json.dumps(cap), manifest)["capitalization"] is False


def test_repeated_trial_scoring():
    _, manifest = h.generate_fixture("small-8k")
    scores = h.score_trials([json.dumps(manifest["expected_answers"]), "not json"], manifest)
    assert scores["trial_count"] == 2
    assert scores["exact_match_count"] == 1
    assert scores["pass_rate"] == 0.5
    assert scores["failure_categories"]["not_json_only"] == 1


def test_progress_invariants_success_and_failures():
    ok = [
        {"sequence":1,"phase":"preparing","total_prompt_tokens":10,"processed_prompt_tokens":0,"generated_tokens":0},
        {"sequence":2,"phase":"prefill","total_prompt_tokens":10,"processed_prompt_tokens":5,"generated_tokens":0},
        {"sequence":3,"phase":"generating","total_prompt_tokens":10,"processed_prompt_tokens":10,"generated_tokens":1},
        {"sequence":4,"phase":"completed","total_prompt_tokens":10,"processed_prompt_tokens":10,"generated_tokens":2},
    ]
    assert h.analyze_progress(ok, "completed")["pass"] is True
    bad = ok + [{"sequence":3,"phase":"prefill","total_prompt_tokens":11,"processed_prompt_tokens":12,"generated_tokens":0}]
    result = h.analyze_progress(bad)
    assert result["pass"] is False
    assert "progress_after_terminal" in result["errors"]


def test_phase_timing_throughput():
    m = h.summarize_metrics(0, 2, 5, 100, 6)
    assert m["prompt_tokens_per_s"] == 50
    assert m["decode_tokens_per_s"] == 2


def test_kv_compare_boundaries_and_fallback():
    est = {"exact_kv_allocation_bytes": 10000, "kv_cache_breakdown": {"exact_allocation_available": True}}
    assert h.compare_kv_estimate(est, {"kv_allocation_bytes": 14096})["pass"] is True
    assert h.compare_kv_estimate(est, {"kv_allocation_bytes": 14097})["pass"] is False
    assert h.compare_kv_estimate({"fallback": True}, {"kv_allocation_bytes": 1})["code"] == "exact_kv_diagnostics_absent_or_fallback"


def test_memory_probe_success_absent_timeout_malformed_and_sanitize(tmp_path):
    good = tmp_path/"good.py"; good.write_text('import json; print(json.dumps({"rss_bytes": 7, "path":"/Users/alice/secret"}))')
    assert h.platform_memory_probe([sys.executable, str(good)])["available"] is True
    missing = h.platform_memory_probe([str(tmp_path/"missing")])
    assert missing["code"] == "probe_absent"
    slow = tmp_path/"slow.py"; slow.write_text('import time; time.sleep(9)')
    assert h.platform_memory_probe([sys.executable, str(slow)], timeout_s=0.1)["code"] == "probe_timeout"
    malformed = tmp_path/"bad.py"; malformed.write_text('print("secret=abc /Users/alice/file")')
    assert "<redacted>" in h.platform_memory_probe([sys.executable, str(malformed)])["stdout_tail"]


def test_atomic_report_schema_and_redaction(tmp_path):
    path = h.write_report_atomic(tmp_path, {"prompt":"secret", "runtime":{"path":"/Users/alice/model.gguf"}, "ok": True})
    data = json.loads(path.read_text())
    assert data["schema_version"] == h.SCHEMA_VERSION
    assert "prompt" not in data
    assert "<redacted>" in data["runtime"]["path"]


def test_cli_validation_and_evaluate(tmp_path):
    proc = subprocess.run([sys.executable, "scripts/p8_benchmark.py", "packaged-runtime", "--out-dir", str(tmp_path)], text=True, capture_output=True)
    assert proc.returncode == 2
    prompt, manifest = h.generate_fixture("small-8k")
    mf = tmp_path/"m.json"; mf.write_text(json.dumps(manifest))
    resp = tmp_path/"r.json"; resp.write_text(json.dumps(manifest["expected_answers"]))
    proc = subprocess.run([sys.executable, "scripts/p8_benchmark.py", "evaluate", "--manifest", str(mf), "--response", str(resp), "--strict", "--out-dir", str(tmp_path)], text=True, capture_output=True)
    assert proc.returncode == 0


def test_platform_context_behavior():
    assert h.get_context_profile("8k-fast").total_context_tokens == 8192
    assert h.platform.system().lower() in {"linux", "darwin", "windows"}

def test_progress_triggered_cancellation_and_recovery_contracts():
    events = [
        {"sequence":1,"phase":"prefill","total_prompt_tokens":100,"processed_prompt_tokens":10,"generated_tokens":0},
        {"sequence":2,"phase":"prefill","total_prompt_tokens":100,"processed_prompt_tokens":50,"generated_tokens":0},
        {"sequence":3,"phase":"cancelled","total_prompt_tokens":100,"processed_prompt_tokens":50,"generated_tokens":0},
    ]
    ok = h.cancellation_recovery_result(events, phase="prefill", threshold=50, followup_ok=True, cleanup_s=2)
    assert ok["pass"] is True
    bad = h.cancellation_recovery_result(events, phase="generating", threshold=1, followup_ok=False, cleanup_s=31, late_result=True, stale_progress=True)
    assert bad["pass"] is False
    assert "cancel_not_triggered" in bad["errors"]
    assert "cleanup_timeout" in bad["errors"]
    assert "late_result_after_cancel" in bad["errors"]
    assert "stale_progress_after_cancel" in bad["errors"]
    assert "followup_worker_failed" in bad["errors"]
