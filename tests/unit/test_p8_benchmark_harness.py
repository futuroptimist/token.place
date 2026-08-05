import json
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize(
    ("key", "heading"),
    [
        ("XIV", "The Winged Monkeys"),
        ("XXI", "The Lion Becomes the King"),
        ("XIV", "the winged monkeys"),
        ("XXI", "the lion becomes the king"),
        ("XIV", "The Winged Monkeys."),
        ("XXI", "The Lion Becomes the King!"),
        ("XIV", "The   Winged  Monkeys"),
        ("XXI", "The  Lion   Becomes the  King"),
    ],
)
def test_semantic_heading_variants_are_not_prose(key, heading):
    _, manifest = h.generate_fixture("small-8k")
    payload = {**manifest["expected_answers"], key: heading}
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    assert score["prose_not_heading"] is False
    assert "prose_not_heading" in score["errors"]


def test_semantic_arbitrary_wrong_prose_is_not_a_heading():
    _, manifest = h.generate_fixture("small-8k")
    payload = {**manifest["expected_answers"], "VII": "These words are quite wrong"}
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    assert score["prose_not_heading"] is True
    assert score["target_selection"] is False


@pytest.mark.parametrize("payload", [[], 7, None])
def test_semantic_valid_non_object_json_has_complete_closed_score(payload):
    _, manifest = h.generate_fixture("small-8k")
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    assert score["json_only"] is True
    assert all(score[key] is False for key in manifest["scoring_rules"] if key != "json_only")


@pytest.mark.parametrize("response", ["not json", "```json\n{}\n```", '{"VII": "x"} commentary'])
def test_semantic_rejects_invalid_json_fences_and_commentary(response):
    _, manifest = h.generate_fixture("small-8k")
    score = h.evaluate_semantic(response, manifest)
    assert score["json_only"] is False
    assert score["semantic_pass"] is False


@pytest.mark.parametrize("bad_value", [None, 3, [], {}])
def test_semantic_missing_and_non_string_values_fail_closed(bad_value):
    _, manifest = h.generate_fixture("small-8k")
    payload = dict(manifest["expected_answers"])
    payload["VII"] = bad_value
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    for key in ("target_selection", "prose_not_heading", "word_count", "capitalization", "trailing_punctuation", "exact_match", "semantic_pass"):
        assert score[key] is False
    del payload["VII"]
    assert h.evaluate_semantic(json.dumps(payload), manifest)["word_count"] is False


def test_semantic_categories_remain_independent():
    _, manifest = h.generate_fixture("small-8k")
    expected = manifest["expected_answers"]

    internal_case = {**expected, "XIV": "You will Remember there was"}
    score = h.evaluate_semantic(json.dumps(internal_case), manifest)
    assert score["target_selection"] is True
    assert score["capitalization"] is False

    punctuated = {**expected, "XXI": expected["XXI"] + "."}
    score = h.evaluate_semantic(json.dumps(punctuated), manifest)
    assert score["target_selection"] is True
    assert score["trailing_punctuation"] is False

    spaced = {**expected, "VII": "They  were obliged to camp"}
    score = h.evaluate_semantic(json.dumps(spaced), manifest)
    assert score["target_selection"] is True
    assert score["word_count"] is True
    assert score["exact_match"] is False

    wrong_five_words = {**expected, "VII": "These words are quite wrong"}
    score = h.evaluate_semantic(json.dumps(wrong_five_words), manifest)
    assert score["word_count"] is True
    assert score["target_selection"] is False


def test_semantic_wrong_missing_canary_and_key_sets():
    _, manifest = h.generate_fixture("small-8k")
    expected = manifest["expected_answers"]
    assert h.evaluate_semantic(json.dumps({**expected, "canary": "wrong"}), manifest)["canary_exact"] is False
    missing = dict(expected); del missing["canary"]
    score = h.evaluate_semantic(json.dumps(missing), manifest)
    assert score["canary_exact"] is False and score["exact_key_set"] is False
    assert h.evaluate_semantic(json.dumps({**expected, "extra": "x"}), manifest)["exact_key_set"] is False


def test_semantic_score_shape_is_stable_boolean_and_errors_deduplicated():
    _, manifest = h.generate_fixture("small-8k")
    fields = set(manifest["scoring_rules"]) | {"semantic_pass"}
    for response in (json.dumps(manifest["expected_answers"]), "null", "bad"):
        score = h.evaluate_semantic(response, manifest)
        assert fields <= score.keys()
        assert all(type(score[key]) is bool for key in fields)
        assert len(score["errors"]) == len(set(score["errors"]))


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
    assert scores["failure_categories"]["json_only"] == 1


def test_progress_invariants_success_and_failures():
    ok = [
        {"sequence":1,"phase":"preparing","total_prompt_tokens":10,"cached_prompt_tokens":0,"processed_prompt_tokens":0,"generated_tokens":0,"elapsed_ms":0},
        {"sequence":2,"phase":"prefill","total_prompt_tokens":10,"cached_prompt_tokens":2,"processed_prompt_tokens":5,"generated_tokens":0,"elapsed_ms":1},
        {"sequence":3,"phase":"generating","total_prompt_tokens":10,"cached_prompt_tokens":2,"processed_prompt_tokens":10,"generated_tokens":1,"elapsed_ms":2},
    ]
    assert h.analyze_progress(ok, "completed")["pass"] is True
    bad = ok + [{"sequence":3,"phase":"prefill","total_prompt_tokens":11,"cached_prompt_tokens":13,"processed_prompt_tokens":12,"generated_tokens":0,"elapsed_ms":1}]
    result = h.analyze_progress(bad)
    assert result["pass"] is False
    assert "decreasing_sequence" in result["errors"]
    assert "cached_exceeds_processed" in result["errors"]
    assert "decreasing_elapsed" in result["errors"]
    assert "changing_prompt_total" in result["errors"]


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
        {"sequence":1,"phase":"prefill","total_prompt_tokens":100,"cached_prompt_tokens":0,"processed_prompt_tokens":10,"generated_tokens":0,"elapsed_ms":0},
        {"sequence":2,"phase":"prefill","total_prompt_tokens":100,"cached_prompt_tokens":0,"processed_prompt_tokens":50,"generated_tokens":0,"elapsed_ms":1},
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


def test_manifest_scoring_rules_match_score_keys():
    _, manifest = h.generate_fixture("small-8k")
    score = h.evaluate_semantic(json.dumps(manifest["expected_answers"]), manifest)
    assert set(manifest["scoring_rules"]).issubset(score.keys())


def test_phase_timing_allows_zero_first_token():
    m = h.summarize_metrics(0.0, 0.0, 5.0, 100, 6)
    assert m["prefill_duration_s"] == 0.0
    assert m["decode_duration_s"] == 5.0
    assert m["decode_tokens_per_s"] == 1.2


def test_memory_probe_parses_before_sanitizing_long_json(tmp_path):
    probe = tmp_path / "long_probe.py"
    probe.write_text('import json; print(json.dumps({"padding":"' + ('x' * 700) + '", "rss_bytes": 9}))')
    result = h.platform_memory_probe([sys.executable, str(probe)])
    assert result["available"] is True
    assert result["payload"]["rss_bytes"] == 9
    assert len(result["payload"]["padding"]) == 512


def test_report_redacts_authorization_and_message_like_payloads(tmp_path):
    path = h.write_report_atomic(tmp_path, {
        "diagnostics": "Authorization: Bearer sk-secret api_key = sk-other",
        "adapter": {
            "messages": [{"content": "plain prompt"}],
            "tool_arguments": {"secret": "plain args"},
            "model_output": "plain output",
            "safe": "Authorization: Bearer sk-nested",
        },
    })
    text = path.read_text()
    data = json.loads(text)
    assert "sk-secret" not in text
    assert "sk-other" not in text
    assert "plain prompt" not in text
    assert "plain args" not in text
    assert "plain output" not in text
    assert "messages" not in data["adapter"]
    assert data["adapter"]["safe"] == "<redacted>"


def test_packaged_runtime_invokes_repository_runner_and_cleans_files(tmp_path):
    _, manifest = h.generate_fixture("small-8k")
    model = tmp_path / "model.gguf"
    model.write_bytes(b"test artifact")
    payload = {
        "response_text": json.dumps(manifest["expected_answers"]),
        "progress_events": [
            {"sequence": 1, "phase": "preparing", "total_prompt_tokens": manifest["actual_tokens"], "cached_prompt_tokens": 0, "processed_prompt_tokens": 0, "generated_tokens": 0, "elapsed_ms": 0},
            {"sequence": 2, "phase": "generating", "total_prompt_tokens": manifest["actual_tokens"], "cached_prompt_tokens": 0, "processed_prompt_tokens": manifest["actual_tokens"], "generated_tokens": 4, "elapsed_ms": 2000},
        ],
        "terminal": "completed",
        "start_s": 0.0,
        "first_token_s": 0.0,
        "end_s": 2.0,
        "output_tokens": 4,
        "messages": [{"content": "plaintext"}],
        "memory": {"diagnostic": "Authorization: Bearer sk-runtime"},
        "app_identity": "token.place-test",
        "runtime_identity": "bundled-test",
        "bundled_runtime_identity": "bundled-test",
        "build_identity": "unit-test",
        "backend_used": "metal",
        "model_fingerprint": "sha256:test",
        "authoritative_prompt_tokens": manifest["actual_tokens"],
    }
    app = tmp_path / "app"; app.write_text("app"); app.chmod(0o700)
    seen = {}
    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        request_path = command[command.index("--p8-request") + 1]
        evidence_path = command[command.index("--p8-evidence") + 1]
        seen["request"] = json.loads(h.Path(request_path).read_text())
        h.Path(evidence_path).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0, "", "")

    result = h.invoke_packaged_runtime_adapter(timeout_s=1.5, app_binary=str(app), model=str(model),
        backend="metal", relay_url="https://relay.example", cleanup_timeout_s=3.0,
        subprocess_run=fake_run)
    assert seen["request"]["fixture_id"] == "small-8k"
    assert seen["request"]["prompt"] not in json.dumps(result)
    assert result["runner_kind"] == "repository_packaged_desktop_webdriver"
    assert result["pass"] is True
    assert result["memory"]["diagnostic"] == "<redacted>"
    assert "messages" not in result
    assert not h.Path(seen["command"][seen["command"].index("--p8-request") + 1]).exists()
    assert not h.Path(seen["command"][seen["command"].index("--p8-evidence") + 1]).exists()


def test_packaged_runtime_requires_physical_prerequisites():
    result = h.invoke_packaged_runtime_adapter(timeout_s=1.5)
    assert result["pass"] is False
    assert result["code"] == "packaged_prerequisites_missing"
    assert set(result["missing"]) == {"app_binary", "model", "backend", "relay_url", "cleanup_timeout_s"}


def test_packaged_runtime_validates_app_model_backend_and_relay(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    common = dict(timeout_s=1, app_binary=str(app), model=str(model), backend="metal", relay_url="https://relay.example", cleanup_timeout_s=1)
    assert h.invoke_packaged_runtime_adapter(**{**common, "model": str(tmp_path / "absent.gguf")})["code"] == "model_artifact_invalid"
    assert h.invoke_packaged_runtime_adapter(**{**common, "app_binary": str(tmp_path / "absent")})["code"] == "packaged_app_invalid"
    assert h.invoke_packaged_runtime_adapter(**{**common, "backend": "cpu"})["code"] == "backend_unsupported"
    for url in ("http://relay.example", "ftp://relay.example", "https://user:pw@relay.example", "https://relay.example/#fragment", "https://relay.example:bad"):
        assert h.invoke_packaged_runtime_adapter(**{**common, "relay_url": url})["code"] == "relay_url_invalid"
    assert h._valid_relay_url("http://127.0.0.1:8000")
    assert h._valid_relay_url("https://relay.example")


def test_report_only_does_not_suppress_runtime_failure(tmp_path):
    proc = subprocess.run([
        sys.executable, "scripts/p8_benchmark.py", "packaged-runtime",
        "--out-dir", str(tmp_path), "--app-binary", str(tmp_path / "missing-app"),
        "--model", str(tmp_path / "missing.gguf"), "--backend", "metal",
        "--relay-url", "http://127.0.0.1:8000", "--cleanup-timeout", "1",
        "--report-only",
    ], text=True, capture_output=True)
    assert proc.returncode == 1
