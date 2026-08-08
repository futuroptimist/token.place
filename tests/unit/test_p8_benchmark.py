import json
from pathlib import Path

import pytest

from scripts import p8_benchmark as p8


def fixture():
    fx = p8.generate_fixture("small-8k", seed="unit")
    p8.validate_manifest(fx)
    return fx


def good_response():
    return json.dumps(p8.EXPECTED, separators=(", ", ": "))


def test_fixture_generation_stable_hash_and_depths():
    a = p8.generate_fixture("small-8k", seed="unit")
    b = p8.generate_fixture("small-8k", seed="unit")
    assert a.prompt == b.prompt
    assert a.manifest["fixture_sha256"] == b.manifest["fixture_sha256"]
    assert a.manifest["requested_token_count"] == 8192
    assert a.manifest["actual_token_count"] >= 8192
    labels = [v["target_depth_label"] for v in a.manifest["target_depths"].values()]
    assert labels == ["early", "middle", "late"]
    assert "TABLE OF CONTENTS" in a.prompt
    assert "The Winged Monkeys" in a.prompt
    assert p8.EXPECTED["canary"] in a.prompt


def test_manifest_validation_rejects_hash_tamper():
    fx = fixture()
    bad = p8.Fixture(fx.fixture_id, fx.prompt + "x", fx.manifest)
    with pytest.raises(ValueError, match="hash"):
        p8.validate_manifest(bad)


def test_semantic_accepts_exact_oracle():
    result = p8.evaluate_semantic(good_response(), fixture().manifest)
    assert result["semantic_pass"] is True
    assert all(result["categories"].values())


@pytest.mark.parametrize("bad,category", [
    ({**p8.EXPECTED, "VII": "They were obliged to camp out"}, "word_count"),
    ({**p8.EXPECTED, "XIV": "The Winged Monkeys"}, "chapter_selection"),
    ({**p8.EXPECTED, "XXI": "The Lion Becomes the King"}, "prose_not_heading"),
    ({**p8.EXPECTED, "canary": "wrong"}, "canary"),
    ({"VII": p8.EXPECTED["VII"]}, "exact_key_set"),
    ({**p8.EXPECTED, "extra": "x"}, "exact_key_set"),
    ({**p8.EXPECTED, "VII": "they were obliged to camp"}, "capitalization"),
    ({**p8.EXPECTED, "VII": "They were obliged to camp."}, "trailing_punctuation"),
])
def test_semantic_known_bad_categories(bad, category):
    result = p8.evaluate_semantic(json.dumps(bad), fixture().manifest)
    assert result["semantic_pass"] is False
    assert result["categories"][category] is False


@pytest.mark.parametrize("text,error", [("```json\n{}\n```", "not_json_only"), ("{} commentary", "not_json_only"), ("{bad", "invalid_json")])
def test_invalid_json_markdown_and_commentary(text, error):
    result = p8.evaluate_semantic(text, fixture().manifest)
    assert result["semantic_pass"] is False
    assert result["error_code"] == error


def test_repeated_trial_scoring():
    fx = fixture()
    bad = json.dumps({**p8.EXPECTED, "canary": "wrong"})
    score = p8.score_trials([good_response(), bad], fx.manifest)
    assert score["trial_count"] == 2
    assert score["exact_match_count"] == 1
    assert score["pass_rate"] == 0.5
    assert score["category_pass_counts"]["canary"] == 1


def test_progress_metrics_and_invariants():
    events = [
        {"sequence": 1, "phase": "preparing", "elapsed_seconds": 0, "processed_tokens": 0, "generated_tokens": 0, "prompt_total_tokens": 100},
        {"sequence": 2, "phase": "prefill", "elapsed_seconds": 1, "processed_tokens": 50, "generated_tokens": 0, "prompt_total_tokens": 100},
        {"sequence": 3, "phase": "generation", "elapsed_seconds": 3, "processed_tokens": 100, "generated_tokens": 1, "prompt_total_tokens": 100},
        {"sequence": 4, "phase": "completed", "elapsed_seconds": 5, "processed_tokens": 100, "generated_tokens": 5, "prompt_total_tokens": 100},
    ]
    m = p8.phase_metrics(events, 5, 10)
    assert m["prompt_tokens_per_second"] == 50
    assert m["decode_tokens_per_second"] == 2.5
    assert m["remaining_completion_margin_seconds"] == 5


@pytest.mark.parametrize("events,msg", [
    ([{"sequence": 2, "phase": "prefill", "processed_tokens": 1}, {"sequence": 1, "phase": "prefill", "processed_tokens": 2}], "decreasing_sequence"),
    ([{"sequence": 1, "phase": "prefill", "processed_tokens": 2}, {"sequence": 2, "phase": "prefill", "processed_tokens": 1}], "decreasing_processed"),
    ([{"sequence": 1, "phase": "prefill", "processed_tokens": 11, "prompt_total_tokens": 10}], "processed_exceeds_total"),
    ([{"sequence": 1, "phase": "generation", "processed_tokens": 1}, {"sequence": 2, "phase": "prefill", "processed_tokens": 2}], "invalid_phase_transition"),
    ([{"sequence": 1, "phase": "completed", "processed_tokens": 1}, {"sequence": 2, "phase": "generation", "processed_tokens": 1}], "progress_after_terminal"),
])
def test_progress_rejects_bad_streams(events, msg):
    with pytest.raises(ValueError, match=msg):
        p8.assert_progress_invariants(events)


def test_kv_comparison_boundaries():
    estimate = {"exact_kv_allocation_bytes": 1000, "conservative_fallback_used": False}
    assert p8.compare_kv_estimate(estimate, {"kv_allocation_bytes": 1000})["comparison_pass"]
    assert p8.compare_kv_estimate(estimate, {"kv_allocation_bytes": 1004}, tolerance_bytes=4)["comparison_pass"]
    assert not p8.compare_kv_estimate(estimate, {"kv_allocation_bytes": 1005}, tolerance_bytes=4)["comparison_pass"]
    with pytest.raises(ValueError):
        p8.compare_kv_estimate({"conservative_fallback_used": True}, {"kv_allocation_bytes": 1})


def test_report_redaction_and_atomic_write(tmp_path):
    out = tmp_path / "report.json"
    p8.atomic_write_json(out, {"schema_version": p8.SCHEMA_VERSION, "model_path": "/home/user/model.gguf", "ok": True})
    data = json.loads(out.read_text())
    assert "model_path" not in data
    assert data["ok"] is True


def test_cli_generate_eval_and_fail_closed_packaged(tmp_path):
    assert p8.main(["generate-fixture", "--size", "small-8k", "--output-dir", str(tmp_path)]) == 0
    manifest = tmp_path / "synthetic-small-8k.manifest.json"
    resp = tmp_path / "response.json"
    resp.write_text(good_response())
    assert p8.main(["eval-response", "--manifest", str(manifest), "--response", str(resp), "--output-dir", str(tmp_path)]) == 0
    resp.write_text(json.dumps({**p8.EXPECTED, "canary": "wrong"}))
    assert p8.main(["eval-response", "--manifest", str(manifest), "--response", str(resp), "--output-dir", str(tmp_path), "--report-only"]) == 0
    assert p8.main(["run", "--runtime", "packaged", "--output-dir", str(tmp_path), "--bridge", str(tmp_path / "missing.py")]) == p8.EXIT_INPUT_FAILURE


def test_memory_adapter_success_absence_timeout_malformed_and_sanitization():
    ok = p8.probe_platform_memory(lambda: {"rss_bytes": 10, "path": "/home/user/private"})
    assert ok["available"] is True
    assert "path" not in ok["payload"]
    assert p8.probe_platform_memory(lambda: (_ for _ in ()).throw(FileNotFoundError()))["error_code"] == "probe_absent"
    assert p8.probe_platform_memory(lambda: (_ for _ in ()).throw(TimeoutError()))["error_code"] == "timeout"
    assert p8.probe_platform_memory(lambda: "bad")["error_code"] == "malformed_output"


def test_progress_triggered_cancellation_recovery_invariants():
    events = [
        {"sequence": 1, "phase": "prefill", "processed_tokens": 100},
        {"sequence": 2, "phase": "cancel_requested", "trigger_phase": "prefill"},
        {"sequence": 3, "phase": "canceled"},
    ]
    result = p8.validate_cancellation_scenario(events, trigger_phase="prefill", followup_ok=True, cleanup_seconds=1, cleanup_budget_seconds=5)
    assert result["cancellation_acknowledged"]
    for bad, msg in [
        (events + [{"sequence": 4, "phase": "completed"}], "late_result_after_cancellation"),
        ([events[0], events[1]], "cancellation_not_acknowledged"),
    ]:
        with pytest.raises(ValueError, match=msg):
            p8.validate_cancellation_scenario(bad, trigger_phase="prefill", followup_ok=True, cleanup_seconds=1, cleanup_budget_seconds=5)
    with pytest.raises(ValueError, match="clean_worker_followup_failed"):
        p8.validate_cancellation_scenario(events, trigger_phase="prefill", followup_ok=False, cleanup_seconds=1, cleanup_budget_seconds=5)
    gen_events = [{"sequence": 1, "phase": "generation", "generated_tokens": 2}, {"sequence": 2, "phase": "cancel_requested", "trigger_phase": "generation"}, {"sequence": 3, "phase": "canceled"}]
    assert p8.validate_cancellation_scenario(gen_events, trigger_phase="generation", followup_ok=True, cleanup_seconds=1, cleanup_budget_seconds=5)["followup_request_succeeded"]
