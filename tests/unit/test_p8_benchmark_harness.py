import json
import signal
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
    assert m1["scenario"] == "structured-extraction"
    assert set(m1["target_depths_tokens"]) == {"VII", "XIV", "XXI", "canary"}
    h.validate_manifest(m1, p1)
    assert "The Winged Monkeys" in p1 and "Table of Contents" in p1


def test_fixture_generation_sizes_with_ci_tokenizer():
    for fixture in ("small-8k", "intermediate-32k", "long-55k"):
        _, manifest = h.generate_fixture(fixture)
        assert manifest["requested_tokens"] <= manifest["actual_tokens"] + 300
        assert manifest["actual_tokens"] > manifest["requested_tokens"] - 500


@pytest.mark.parametrize("scenario", ["single-needle", "structured-extraction"])
def test_small_fixture_fits_8k_fast_effective_prompt_budget(scenario):
    first_prompt, first = h.generate_fixture("small-8k", scenario=scenario)
    second_prompt, second = h.generate_fixture("small-8k", scenario=scenario)
    profile = h.get_context_profile("8k-fast")
    prompt_budget = profile.total_context_tokens - profile.default_output_reservation_tokens

    assert profile.total_context_tokens == 8192
    assert profile.default_output_reservation_tokens == 1024
    assert first["requested_tokens"] == prompt_budget == 7168
    assert 0.90 * prompt_budget <= first["actual_tokens"] <= prompt_budget
    assert (first_prompt, first["fixture_sha256"]) == (second_prompt, second["fixture_sha256"])


@pytest.mark.parametrize(("fixture", "depth"), [
    ("small-8k", 0.18), ("intermediate-32k", 0.50), ("long-55k", 0.82),
])
def test_single_needle_and_hidden_canary_have_controlled_depth(fixture, depth):
    prompt, manifest = h.generate_fixture(fixture, scenario="single-needle")
    needle = prompt.split("NEEDLE FACT: ", 1)[1].splitlines()[0]
    assert prompt.count(needle) == 1
    assert manifest["expected_answers"] == {"needle": needle}
    assert manifest["targets"]["needle"]["actual_ratio"] == pytest.approx(depth, abs=0.015)


def test_structured_canary_is_hidden_and_single_occurrence():
    prompt, manifest = h.generate_fixture("small-8k", scenario="structured-extraction")
    canary = manifest["expected_answers"]["canary"]
    assert prompt.count(canary) == 1
    assert canary not in prompt.split("Table of Contents", 1)[0]


def test_fixture_seed_changes_bytes_but_remains_deterministic():
    first = h.generate_fixture("small-8k", "one")
    second = h.generate_fixture("small-8k", "two")
    assert first[0] != second[0]
    assert first[1]["fixture_sha256"] != second[1]["fixture_sha256"]


def test_manifest_validation_rejects_tampering():
    prompt, manifest = h.generate_fixture("small-8k")
    for mutate, code in [
        (lambda value: value.update(fixture_version="old"), "manifest_identity_invalid"),
        (lambda value: value.update(fixture_sha256="0" * 64), "fixture_hash_mismatch"),
        (lambda value: value["expected_answers"].pop("VII"), "manifest_oracle_invalid"),
        (lambda value: value["token_count_provenance"].update(authoritative=True), "manifest_token_provenance_invalid"),
        (lambda value: value["targets"].pop("VII"), "manifest_targets_invalid"),
    ]:
        candidate = json.loads(json.dumps(manifest))
        mutate(candidate)
        with pytest.raises(ValueError, match=code):
            h.validate_manifest(candidate, prompt)


def test_supplied_tokenizer_hook_used_without_claiming_authority():
    _, manifest = h.generate_fixture("small-8k", tokenizer=lambda text: len(text.split()) + 7)
    assert manifest["tokenizer"] == "supplied-callback"
    assert manifest["token_count_provenance"]["authoritative"] is False
    assert 0.90 * 7168 <= manifest["actual_tokens"] <= 7168


def test_semantic_exact_success():
    _, manifest = h.generate_fixture("small-8k")
    response = json.dumps(manifest["expected_answers"])
    score = h.evaluate_semantic(response, manifest)
    assert score["semantic_pass"] is True
    assert score["exact_match"] is True


@pytest.mark.parametrize(("payload", "failed"), [
    ({"needle": "wrong"}, "needle_exact"),
    ({}, "exact_key_set"),
    ({"needle": "wrong", "extra": "value"}, "exact_key_set"),
])
def test_single_needle_oracle_scores_retrieval(payload, failed):
    _, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    score = h.evaluate_semantic(json.dumps(payload), manifest)
    assert score[failed] is False
    assert score["semantic_pass"] is False
    assert failed in score["errors"]


def test_single_needle_oracle_is_deterministic_across_trials():
    _, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    response = json.dumps(manifest["expected_answers"])
    assert h.score_trials([response, response, response], manifest)["exact_match_count"] == 3


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
    lifecycle = ok + [{"kind":"result","status":"success","sequence":4,"elapsed_ms":3},
        {"kind":"terminal","state":"completed","sequence":5,"elapsed_ms":4}]
    assert h.analyze_progress(lifecycle)["pass"] is True
    bad = ok + [{"sequence":3,"phase":"prefill","total_prompt_tokens":11,"cached_prompt_tokens":13,"processed_prompt_tokens":12,"generated_tokens":0,"elapsed_ms":1}]
    result = h.analyze_progress(bad)
    assert result["pass"] is False
    assert "decreasing_sequence" in result["errors"]
    assert "cached_exceeds_processed" in result["errors"]
    assert "decreasing_elapsed" in result["errors"]
    assert "changing_prompt_total" in result["errors"]


def test_phase_timing_throughput():
    m = h.summarize_metrics(start_s=0, preparing_end_s=1, prefill_end_s=3,
        first_token_s=3, end_s=6, prompt_tokens=100, output_tokens=6, request_budget_s=10)
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
    path = h.write_report_atomic(tmp_path, {"mode":"semantic-evaluation", "status":"passed",
        "fixture":{"id":"small-8k", "version":h.FIXTURE_VERSION,
            "scenario":"single-needle", "sha256":"abc"},
        "semantic":{"semantic_pass":True}, "prompt":"secret"})
    data = json.loads(path.read_text())
    assert data["schema_version"] == h.SCHEMA_VERSION
    assert "prompt" not in data


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
    m = h.summarize_metrics(start_s=0.0, preparing_end_s=0.0, prefill_end_s=0.0,
        first_token_s=0.0, end_s=5.0, prompt_tokens=100, output_tokens=6, request_budget_s=5.0)
    assert m["prefill_duration_s"] == 0.0
    assert m["decode_duration_s"] == 5.0
    assert m["decode_tokens_per_s"] == 1.2


def _completed_lifecycle():
    return [
        {"sequence":1,"phase":"preparing","total_prompt_tokens":10,"cached_prompt_tokens":0,
         "processed_prompt_tokens":0,"generated_tokens":0,"elapsed_ms":0},
        {"sequence":2,"phase":"prefill","total_prompt_tokens":10,"cached_prompt_tokens":1,
         "processed_prompt_tokens":10,"generated_tokens":0,"elapsed_ms":1},
        {"sequence":3,"phase":"generating","total_prompt_tokens":10,"cached_prompt_tokens":1,
         "processed_prompt_tokens":10,"generated_tokens":1,"elapsed_ms":2},
        {"kind":"result","status":"success","sequence":4,"elapsed_ms":3},
        {"kind":"terminal","state":"completed","sequence":5,"elapsed_ms":4},
    ]


@pytest.mark.parametrize(("mutate", "error"), [
    (lambda items: items.clear(), "progress_missing"),
    (lambda items: items[0].pop("total_prompt_tokens"), "malformed_telemetry"),
    (lambda items: items[1].update(sequence=1), "decreasing_sequence"),
    (lambda items: items[1].update(elapsed_ms=0), "decreasing_elapsed"),
    (lambda items: items[1].update(processed_prompt_tokens=-1), "malformed_telemetry"),
    (lambda items: items[2].update(generated_tokens=-1), "malformed_telemetry"),
    (lambda items: (items[1].update(processed_prompt_tokens=10),
        items[2].update(processed_prompt_tokens=9)), "decreasing_processed"),
    (lambda items: (items[1].update(generated_tokens=2),
        items[2].update(generated_tokens=1)), "decreasing_generated"),
    (lambda items: items[1].update(total_prompt_tokens=11), "changing_prompt_total"),
    (lambda items: items[0].update(total_prompt_tokens=0), "invalid_prompt_total"),
    (lambda items: items[1].update(processed_prompt_tokens=11), "processed_exceeds_total"),
    (lambda items: items[1].update(phase="generating"), "invalid_phase_transition"),
    (lambda items: (items[1].update(processed_prompt_tokens=9),
        items[2].update(processed_prompt_tokens=9)), "incomplete_prefill"),
    (lambda items: items.append({"sequence":6,"phase":"generating","total_prompt_tokens":10,
        "cached_prompt_tokens":1,"processed_prompt_tokens":10,"generated_tokens":2,"elapsed_ms":5}),
        "progress_after_terminal"),
    (lambda items: items.append({"kind":"terminal","state":"failed","sequence":6,"elapsed_ms":5}),
        "duplicate_terminal"),
    (lambda items: items[-1].update(elapsed_ms=2), "decreasing_elapsed"),
])
def test_ordered_progress_lifecycle_failures(mutate, error):
    lifecycle = _completed_lifecycle()
    mutate(lifecycle)
    result = h.analyze_progress(lifecycle)
    assert result["pass"] is False
    assert error in result["errors"]


def test_cancellation_rejects_late_result():
    lifecycle = _completed_lifecycle()[:3] + [
        {"kind":"terminal","state":"cancelled","sequence":4,"elapsed_ms":3},
        {"kind":"result","status":"success","sequence":5,"elapsed_ms":4},
    ]
    result = h.analyze_progress(lifecycle)
    assert {"result_after_terminal", "result_after_cancellation"}.issubset(result["errors"])


def test_completed_generating_only_lifecycle_requires_prefill():
    lifecycle = [
        {"sequence":1,"phase":"generating","total_prompt_tokens":10,
         "cached_prompt_tokens":0,"processed_prompt_tokens":10,"generated_tokens":1,"elapsed_ms":0},
        {"kind":"result","status":"success","sequence":2,"elapsed_ms":1},
        {"kind":"terminal","state":"completed","sequence":3,"elapsed_ms":2},
    ]
    result = h.analyze_progress(lifecycle)
    assert result["pass"] is False
    assert "prefill_phase_missing" in result["errors"]


def test_completed_lifecycle_may_begin_with_prefill():
    lifecycle = _completed_lifecycle()[1:]
    for sequence, observation in enumerate(lifecycle, start=1):
        observation["sequence"] = sequence
    assert h.analyze_progress(lifecycle)["pass"] is True


def test_missing_prefill_cannot_become_zero_duration_passing_metrics():
    lifecycle = _completed_lifecycle()
    lifecycle.pop(1)
    result = h.analyze_progress(lifecycle)
    assert result["pass"] is False
    assert "prefill_phase_missing" in result["errors"]


@pytest.mark.parametrize(("change", "code"), [
    ({"end_s": float("nan")}, "timing_non_finite"),
    ({"prefill_end_s": 3, "first_token_s": 2}, "timing_order_invalid"),
    ({"end_s": 11}, "request_budget_exceeded"),
])
def test_timing_fails_closed(change, code):
    values = dict(start_s=0, preparing_end_s=1, prefill_end_s=2, first_token_s=2,
        end_s=5, prompt_tokens=100, output_tokens=6, request_budget_s=10)
    assert h.summarize_metrics(**{**values, **change})["code"] == code


def test_timing_reports_every_duration_throughput_budget_and_margin():
    metrics = h.summarize_metrics(start_s=1, preparing_end_s=2, prefill_end_s=4,
        first_token_s=5, end_s=8, prompt_tokens=100, output_tokens=6, request_budget_s=10)
    assert metrics == {"pass":True, "preparing_duration_s":1, "prefill_duration_s":2,
        "time_to_first_token_s":4, "decode_duration_s":3, "total_duration_s":7,
        "prompt_tokens":100, "output_tokens":6, "prompt_tokens_per_s":50,
        "decode_tokens_per_s":2, "request_budget_s":10, "completion_margin_s":3}


def test_invalid_report_preserves_existing_atomic_destination(tmp_path):
    destination = tmp_path / "p8_benchmark_report.json"
    destination.write_text("existing")
    with pytest.raises(ValueError, match="report_schema_missing"):
        h.write_report_atomic(tmp_path, {"mode":"packaged-runtime"})
    assert destination.read_text() == "existing"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_report_schema_rejects_non_finite_values(tmp_path, bad):
    with pytest.raises(ValueError, match="report_non_finite"):
        h.write_report_atomic(tmp_path, {"mode":"semantic-evaluation", "status":"passed",
            "fixture":{"id":"small", "version":h.FIXTURE_VERSION,
                "scenario":"single-needle", "sha256":"abc"},
            "semantic":{"semantic_pass":True, "pass_rate":bad}})


def test_post_terminal_observation_is_clock_bounded():
    now = [0.0]; sleeps = []
    def sleep(value):
        sleeps.append(value); now[0] += value
    observed = h.observe_post_terminal(lambda: "poll", clock=lambda: now[0],
        sleeper=sleep, window_s=0.1, interval_s=0.05)
    assert observed == ["poll", "poll"]
    assert all(0 <= value <= 0.05 for value in sleeps)


def test_memory_probe_parses_before_sanitizing_long_json(tmp_path):
    probe = tmp_path / "long_probe.py"
    probe.write_text('import json; print(json.dumps({"padding":"' + ('x' * 700) + '", "rss_bytes": 9}))')
    result = h.platform_memory_probe([sys.executable, str(probe)])
    assert result["available"] is True
    assert result["payload"]["rss_bytes"] == 9
    assert len(result["payload"]["padding"]) == 512


def test_report_redacts_authorization_and_message_like_payloads(tmp_path):
    path = h.write_report_atomic(tmp_path, {
        "mode":"semantic-evaluation", "status":"passed",
        "fixture":{"id":"small-8k", "version":h.FIXTURE_VERSION,
            "scenario":"single-needle", "sha256":"abc"},
        "semantic":{"semantic_pass":True},
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


def test_packaged_runtime_loads_valid_external_fixture_and_cleans_files(tmp_path):
    prompt, manifest = h.generate_fixture("small-8k")
    authoritative_total = manifest["actual_tokens"] + 17
    authoritative_offsets = {key: round(value["actual_ratio"] * authoritative_total)
        for key, value in manifest["targets"].items()}
    model = tmp_path / "model.gguf"
    model.write_bytes(b"test artifact")
    payload = {
        "response_text": json.dumps(manifest["expected_answers"]),
        "progress_events": [
            {"sequence": 1, "phase": "preparing", "total_prompt_tokens": authoritative_total, "cached_prompt_tokens": 0, "processed_prompt_tokens": 0, "generated_tokens": 0, "elapsed_ms": 0},
            {"sequence": 2, "phase": "prefill", "total_prompt_tokens": authoritative_total, "cached_prompt_tokens": 0, "processed_prompt_tokens": authoritative_total, "generated_tokens": 0, "elapsed_ms": 1000},
            {"sequence": 3, "phase": "generating", "total_prompt_tokens": authoritative_total, "cached_prompt_tokens": 0, "processed_prompt_tokens": authoritative_total, "generated_tokens": 4, "elapsed_ms": 2000},
        ],
        "result_observation": {"kind":"result", "status":"success", "sequence":4, "elapsed_ms":2001},
        "terminal_observation": {"kind":"terminal", "state":"completed", "sequence":5, "elapsed_ms":2002},
        "post_terminal_observations": [], "start_s": 0.0, "preparing_end_s": 0.0,
        "prefill_end_s": 1.0, "first_token_s": 1.0, "end_s": 2.0,
        "output_tokens": 4,
        "messages": [{"content": "plaintext"}],
        "memory": {"diagnostic": "Authorization: Bearer sk-runtime"},
        "app_identity": "token.place-test",
        "runtime_identity": "bundled-test",
        "bundled_runtime_identity": "bundled-test",
        "build_identity": "unit-test",
        "backend_requested": "metal", "backend_selected": "metal", "backend_used": "metal",
        "model_fingerprint": "sha256:test",
        "authoritative_prompt_tokens": authoritative_total,
        "authoritative_tokenizer_evidence": {"method": "packaged_admission_render_and_tokenize_chat", "runtime_identity": "bundled-test", "fixture_sha256": manifest["fixture_sha256"], "total_prompt_tokens": authoritative_total, "target_offsets_tokens": authoritative_offsets},
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

    result = h.invoke_packaged_runtime_adapter(timeout_s=3.0, app_binary=str(app), model=str(model),
        backend="metal", relay_url="https://relay.example", cleanup_timeout_s=3.0,
        external_prompt=prompt, external_manifest=manifest, subprocess_run=fake_run)
    assert seen["request"]["fixture_id"] == "small-8k"
    assert seen["request"]["prompt"] not in json.dumps(result)
    assert result["runner_kind"] == "repository_packaged_desktop_webdriver"
    assert result["pass"] is True
    assert result["fixture"]["estimated_prompt_tokens"] != result["fixture"]["authoritative_prompt_tokens"]
    assert result["fixture"]["authoritative_target_offsets_tokens"] == authoritative_offsets
    assert result["memory"]["diagnostic"] == "<redacted>"
    assert "messages" not in result
    assert not h.Path(seen["command"][seen["command"].index("--p8-request") + 1]).exists()
    assert not h.Path(seen["command"][seen["command"].index("--p8-evidence") + 1]).exists()


def test_packaged_runtime_external_fixture_pair_and_hash_fail_closed(tmp_path):
    prompt, manifest = h.generate_fixture("small-8k")
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    common = dict(app_binary=str(app), model=str(model), backend="cpu",
        relay_url="https://relay.example", cleanup_timeout_s=1)
    assert h.invoke_packaged_runtime_adapter(**common, external_prompt=prompt)["code"] == "external_fixture_pair_required"
    assert h.invoke_packaged_runtime_adapter(**common, external_prompt=prompt + "tampered",
        external_manifest=manifest)["code"] == "fixture_hash_mismatch"


@pytest.mark.parametrize(("mutation", "code"), [
    (lambda value: value.update(method="whitespace-ci"), "authoritative_target_depth_malformed"),
    (lambda value: value.update(runtime_identity="other"), "authoritative_target_depth_mismatched"),
    (lambda value: value.update(total_prompt_tokens=99), "authoritative_target_depth_mismatched"),
    (lambda value: value.update(fixture_sha256="0" * 64), "authoritative_target_depth_stale"),
    (lambda value: value.update(target_offsets_tokens={}), "authoritative_target_depth_malformed"),
    (lambda value: value["target_offsets_tokens"].update(XIV=value["target_offsets_tokens"]["VII"]),
     "authoritative_target_depth_ambiguous"),
    (lambda value: value["target_offsets_tokens"].update(
        VII=value["target_offsets_tokens"]["XXI"] - 1),
     "authoritative_target_depth_ordering"),
    (lambda value: value["target_offsets_tokens"].update(VII=1),
     "authoritative_target_depth_ratio"),
])
def test_authoritative_target_depth_evidence_fails_categorically(mutation, code):
    _, manifest = h.generate_fixture("small-8k")
    evidence = {"method": "packaged_admission_render_and_tokenize_chat",
        "runtime_identity": "bundled", "total_prompt_tokens": manifest["actual_tokens"],
        "fixture_sha256": manifest["fixture_sha256"],
        "target_offsets_tokens": {key: value["actual_offset_tokens"]
            for key, value in manifest["targets"].items()}}
    mutation(evidence)
    _, error = h._validate_authoritative_tokenizer_evidence(
        evidence, manifest, "bundled", manifest["actual_tokens"])
    assert error == code


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
    assert h.invoke_packaged_runtime_adapter(**{**common, "backend": "rocm"})["code"] == "backend_unsupported"
    for url in ("http://relay.example", "ftp://relay.example", "https://user:pw@relay.example", "https://relay.example/#fragment", "https://relay.example:bad"):
        assert h.invoke_packaged_runtime_adapter(**{**common, "relay_url": url})["code"] == "relay_url_invalid"
    assert h._valid_relay_url("http://127.0.0.1:8000")
    assert h._valid_relay_url("https://relay.example")


@pytest.mark.parametrize(
    ("runner_outcome", "expected_code"),
    [
        ("timeout", "packaged_runner_timeout"),
        ("failed", "packaged_runner_failed"),
        ("invalid-json", "packaged_evidence_malformed"),
        ("non-object", "packaged_evidence_malformed"),
        ("missing", "authoritative_target_depth_unavailable"),
    ],
)
def test_packaged_runtime_rejects_runner_and_evidence_failures(tmp_path, runner_outcome, expected_code):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    app = tmp_path / "app"
    app.write_text("x")
    app.chmod(0o700)

    def fake_run(command, **kwargs):
        if runner_outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if runner_outcome == "failed":
            return subprocess.CompletedProcess(command, 1, "", "")
        evidence_path = command[command.index("--p8-evidence") + 1]
        evidence = {"invalid-json": "not json", "non-object": "[]", "missing": "{}"}[runner_outcome]
        h.Path(evidence_path).write_text(evidence)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = h.invoke_packaged_runtime_adapter(
        timeout_s=1,
        app_binary=str(app),
        model=str(model),
        backend="metal",
        relay_url="https://relay.example",
        cleanup_timeout_s=1,
        subprocess_run=fake_run,
    )
    assert result["pass"] is False
    assert result["code"] == expected_code


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), "1"])
def test_packaged_runtime_rejects_invalid_timeouts(tmp_path, timeout):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"x")
    app = tmp_path / "app"
    app.write_text("x")
    app.chmod(0o700)
    result = h.invoke_packaged_runtime_adapter(
        timeout_s=timeout,
        app_binary=str(app),
        model=str(model),
        backend="metal",
        relay_url="https://relay.example",
        cleanup_timeout_s=1,
    )
    assert result == {"pass": False, "code": "timeout_invalid"}


def test_report_only_does_not_suppress_runtime_failure(tmp_path):
    proc = subprocess.run([
        sys.executable, "scripts/p8_benchmark.py", "packaged-runtime",
        "--out-dir", str(tmp_path), "--app-binary", str(tmp_path / "missing-app"),
        "--model", str(tmp_path / "missing.gguf"), "--backend", "metal",
        "--relay-url", "http://127.0.0.1:8000", "--cleanup-timeout", "1",
        "--report-only",
    ], text=True, capture_output=True)
    assert proc.returncode == 1


@pytest.mark.parametrize("scenario", ["single-needle", "structured-extraction"])
def test_small_fixture_passes_8k_fast_context_preflight(tmp_path, scenario):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    launched = []
    def fake_run(command, **kwargs):
        launched.append(command)
        return subprocess.CompletedProcess(command, 1, "runner stopped", "")
    result = h.invoke_packaged_runtime_adapter(app_binary=str(app), model=str(model),
        backend="cpu", relay_url="https://relay.example", cleanup_timeout_s=1,
        context_tier="8k-fast", scenario=scenario, subprocess_run=fake_run)
    assert launched
    assert result["code"] == "packaged_runner_failed"


@pytest.mark.parametrize(("report_only", "semantic_ok", "accepted"), [
    (False, False, False), (True, False, True), (True, True, False),
])
def test_report_only_only_accepts_semantic_failure(tmp_path, report_only, semantic_ok, accepted):
    _, manifest = h.generate_fixture("small-8k")
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    response = manifest["expected_answers"] if semantic_ok else {**manifest["expected_answers"], "canary": "wrong"}
    payload = {
        "response_text": json.dumps(response), "start_s": 0.0, "preparing_end_s": 0.0,
        "prefill_end_s": 1.0, "first_token_s": 1.0, "end_s": 2.0, "output_tokens": 4,
        "result_observation":{"kind":"result", "status":"success", "sequence":3, "elapsed_ms":2001},
        "terminal_observation":{"kind":"terminal", "state":"completed", "sequence":4, "elapsed_ms":2002},
        "post_terminal_observations":[],
        "app_identity": "token.place", "runtime_identity": "bundled",
        "bundled_runtime_identity": "bundled", "build_identity": "build",
        "backend_requested": "cpu", "backend_selected": "cpu", "backend_used": "cpu", "model_fingerprint": "sha256:test",
        "authoritative_prompt_tokens": manifest["actual_tokens"],
        "authoritative_tokenizer_evidence": {"method": "packaged_admission_render_and_tokenize_chat", "runtime_identity": "bundled", "fixture_sha256": manifest["fixture_sha256"], "total_prompt_tokens": manifest["actual_tokens"], "target_offsets_tokens": {key: value["actual_offset_tokens"] for key, value in manifest["targets"].items()}},
        "progress_events": [
            {"sequence": 1, "phase": "prefill",
             "total_prompt_tokens": manifest["actual_tokens"], "cached_prompt_tokens": 0,
             "processed_prompt_tokens": manifest["actual_tokens"], "generated_tokens": 0,
             "elapsed_ms": 1000},
            {"sequence": 2, "phase": "generating",
             "total_prompt_tokens": manifest["actual_tokens"], "cached_prompt_tokens": 0,
             "processed_prompt_tokens": manifest["actual_tokens"], "generated_tokens": 4,
             "elapsed_ms": 2000}],
    }
    def fake_run(command, **kwargs):
        h.Path(command[command.index("--p8-evidence") + 1]).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0)
    result = h.invoke_packaged_runtime_adapter(app_binary=str(app), model=str(model), backend="cpu",
        relay_url="https://relay.example", cleanup_timeout_s=1, report_only=report_only,
        subprocess_run=fake_run)
    assert result["runtime_contract_pass"] is True
    assert result["pass"] is semantic_ok
    assert result["report_only_accepted"] is accepted


def test_packaged_temp_permissions_do_not_require_fchmod(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    monkeypatch.delattr(h.os, "fchmod")
    def failed_runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1)
    result = h.invoke_packaged_runtime_adapter(app_binary=str(app), model=str(model), backend="cpu",
        relay_url="https://relay.example", cleanup_timeout_s=1, subprocess_run=failed_runner)
    assert result["code"] == "packaged_runner_failed"


@pytest.mark.parametrize(("state", "expected"), [
    ({"h": [], "b": True}, ("running", None)),
    ({"h": [], "b": False}, ("running", None)),
    ({"h": [{"role": "assistant", "content": "ok", "isTyping": False,
              "finishReason": "stop"}], "b": False}, ("completed", "ok")),
    ({"h": [{"role": "assistant", "content": "fallback"}], "b": False}, ("failed", None)),
    ({"h": [{"role": "assistant", "content": {"error": "bad"}, "isTyping": False,
              "finishReason": "error"}], "b": False}, ("failed", None)),
    ({"h": [{"role": "assistant", "content": "missing lifecycle"}], "b": False}, ("failed", None)),
])
def test_desktop_runner_requires_success_lifecycle(state, expected):
    assert h.classify_p8_landing_state(state) == expected


def test_desktop_runner_applies_tier_and_maps_operator_mode():
    class Browser:
        def execute_script(self, script, tier):
            assert "selectedContextTier" in script
            self.tier = tier
            return tier
    browser = Browser()
    assert h.apply_p8_context_tier(browser, "64k-full") == "64k-full"
    assert browser.tier == "64k-full"
    assert h.p8_operator_mode("cpu") == "cpu"
    assert h.p8_operator_mode("metal") == "gpu"
    assert h.p8_operator_mode("cuda") == "gpu"
    with pytest.raises(ValueError):
        h.p8_operator_mode("mock")


def test_owned_runner_keeps_only_bounded_diagnostic_tail():
    completed = h._run_owned_runner(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000 + 'TAIL')"], 2, 1)
    assert completed.returncode == 0
    assert len(completed.stdout) <= 2048
    assert completed.stdout.endswith("TAIL")


class _TimedOutProcess:
    pid = 731
    stdout = None

    def __init__(self, waits):
        self.waits = iter(waits)
        self.killed = False

    def wait(self, timeout):
        outcome = next(self.waits)
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("runner", timeout)
        return outcome

    def kill(self):
        self.killed = True


def test_owned_runner_posix_terminates_exact_process_group(monkeypatch):
    process = _TimedOutProcess(["timeout", "timeout", -9])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    launched = {}
    signals = []
    with pytest.raises(subprocess.TimeoutExpired):
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda command, **kwargs: launched.update(kwargs) or process,
            killpg=lambda pid, sig: signals.append((pid, sig)), platform_name="posix")
    assert launched["start_new_session"] is True
    assert signals == [(731, signal.SIGTERM), (731, signal.SIGKILL)]


@pytest.mark.parametrize("cleanup_outcome", ["failed", "timeout"])
def test_owned_runner_windows_cleans_exact_pid_and_reaps(cleanup_outcome):
    process = _TimedOutProcess(["timeout", 1] if cleanup_outcome == "failed" else ["timeout", 1])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    launched = {}; cleanup = []
    def cleanup_run(command, **kwargs):
        cleanup.append((command, kwargs))
        if cleanup_outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 1)
    with pytest.raises(subprocess.TimeoutExpired):
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda command, **kwargs: launched.update(kwargs) or process,
            cleanup_run=cleanup_run, platform_name="nt")
    assert launched["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert cleanup[0][0] == ["taskkill", "/PID", "731", "/T", "/F"]
    assert process.killed is (cleanup_outcome == "timeout")


def test_main_generate_and_evaluate_commands(tmp_path):
    fixture_dir = tmp_path / "fixture"
    assert h.main(["generate-fixture", "--fixture", "small-8k", "--scenario", "structured-extraction", "--out-dir", str(fixture_dir)]) == 0
    manifest_path = fixture_dir / "small-8k.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(manifest["expected_answers"]))
    report_dir = tmp_path / "report"
    assert h.main(["evaluate", "--manifest", str(manifest_path), "--response",
        str(response_path), "--strict", "--out-dir", str(report_dir)]) == 0
    assert json.loads((report_dir / "p8_benchmark_report.json").read_text())["semantic"]["semantic_pass"]


def test_main_packaged_runtime_exit_codes(tmp_path, monkeypatch):
    evidence = {"pass": False, "report_only_accepted": True, "runtime_contract_pass":True,
        "fixture":{"sha256":"abc", "authoritative_prompt_tokens":10},
        "runtime":{"app_identity":"token.place", "runtime_identity":"bundled",
            "build_identity":"build", "backend_requested":"cpu", "backend_selected":"cpu", "model_fingerprint":"sha256:model", "backend_used":"cpu"},
        "progress":{"pass":True, "progress_event_count":1},
        "metrics":{"pass":True, "preparing_duration_s":0, "prefill_duration_s":1,
            "time_to_first_token_s":1, "decode_duration_s":1, "total_duration_s":2,
            "prompt_tokens":10, "output_tokens":1, "prompt_tokens_per_s":10,
            "decode_tokens_per_s":1, "request_budget_s":600, "completion_margin_s":598},
        "semantic":{"semantic_pass":False}}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **kwargs: evidence)
    args = ["packaged-runtime", "--out-dir", str(tmp_path), "--app-binary", "app",
        "--model", "model", "--backend", "cpu", "--relay-url", "https://relay.example",
        "--report-only"]
    assert h.main(args) == 0
    report = json.loads((tmp_path / "p8_benchmark_report.json").read_text())
    assert report["overall_pass"] is False
    assert report["semantic"]["semantic_pass"] is False
    assert report["report_only_accepted"] is True

    evidence["report_only_accepted"] = False
    assert h.main(args) == 1
