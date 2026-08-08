from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.p8_benchmark.core import (
    FakePackagedAdapter,
    HarnessError,
    WhitespaceTokenizer,
    atomic_write_report,
    calculate_metrics,
    compare_kv_estimate,
    evaluate_semantic,
    generate_fixture,
    main,
    run_report_only,
    sanitize,
    score_trials,
    validate_platform_backend,
    validate_progress,
)


def good_events():
    return [
        {
            "seq": 1,
            "phase": "preparing",
            "processed_tokens": 0,
            "generated_tokens": 0,
            "total_prompt_tokens": 100,
            "t": 0.0,
        },
        {
            "seq": 2,
            "phase": "prefill",
            "processed_tokens": 50,
            "generated_tokens": 0,
            "total_prompt_tokens": 100,
            "t": 1.0,
        },
        {
            "seq": 3,
            "phase": "prefill",
            "processed_tokens": 100,
            "generated_tokens": 0,
            "total_prompt_tokens": 100,
            "t": 3.0,
        },
        {
            "seq": 4,
            "phase": "generation",
            "processed_tokens": 100,
            "generated_tokens": 2,
            "total_prompt_tokens": 100,
            "t": 4.0,
        },
        {
            "seq": 5,
            "phase": "completed",
            "processed_tokens": 100,
            "generated_tokens": 2,
            "total_prompt_tokens": 100,
            "t": 5.0,
        },
    ]


def manifest():
    return generate_fixture("small-8k", seed=1566)["manifest"]


def oracle_response(m):
    return json.dumps({k: m["expected"][k] for k in m["required_keys"]})


def test_fixture_generation_is_deterministic_and_places_depths():
    a = generate_fixture("small-8k", seed=7)
    b = generate_fixture("small-8k", seed=7)
    assert a["prompt"] == b["prompt"]
    assert a["manifest"]["fixture_sha256"] == b["manifest"]["fixture_sha256"]
    depths = a["manifest"]["target_depths"]
    assert depths["VII"] < depths["XIV"] < depths["XXI"]
    assert a["manifest"]["requested_tokens"] == 8192
    assert a["manifest"]["actual_tokens"] >= 8192


def test_token_count_uses_authoritative_adapter_when_supplied():
    class Tok(WhitespaceTokenizer):
        source = "authoritative"

        def count(self, text):
            return 12345

    fx = generate_fixture("intermediate-32k", tokenizer=Tok())
    assert fx["manifest"]["actual_tokens"] == 12345
    assert fx["manifest"]["tokenizer_source"] == "authoritative"


def test_exact_semantic_evaluation_passes_oracle():
    m = manifest()
    result = evaluate_semantic(oracle_response(m), m)
    assert result["semantic_pass"] is True
    assert all(result["categories"].values())


@pytest.mark.parametrize(
    "bad,code",
    [
        ({"VII": "They were obliged to camp out"}, "word_count_failed"),
        ({"XIV": "The Winged Monkeys"}, "target_selection_failed"),
        ({"XXI": "The Lion Becomes the King"}, "target_selection_failed"),
        ({"canary": "wrong"}, "canary_mismatch"),
    ],
)
def test_known_failures_are_caught(bad, code):
    m = manifest()
    data = {k: m["expected"][k] for k in m["required_keys"]}
    data.update(bad)
    result = evaluate_semantic(json.dumps(data), m)
    assert result["semantic_pass"] is False
    assert code in result["errors"]


@pytest.mark.parametrize(
    "text,code",
    [
        ("not json", "invalid_json"),
        ("```json\n{}\n```", "markdown_or_commentary"),
        (json.dumps({"VII": "They were obliged to camp"}), "key_set_mismatch"),
    ],
)
def test_invalid_json_markdown_and_keyset_fail(text, code):
    result = evaluate_semantic(text, manifest())
    assert result["semantic_pass"] is False
    assert code in result["errors"]


def test_capitalization_punctuation_and_whitespace_categories():
    m = manifest()
    data = {k: m["expected"][k] for k in m["required_keys"]}
    data["VII"] = "they were obliged to camp."
    result = evaluate_semantic(json.dumps(data), m)
    assert result["categories"]["capitalization"] is False
    assert result["categories"]["trailing_punctuation"] is False


def test_repeated_trial_scoring():
    m = manifest()
    summary = score_trials([oracle_response(m), '{"canary":"bad"}'], m)
    assert summary["trial_count"] == 2
    assert summary["exact_match_count"] == 1
    assert summary["pass_rate"] == 0.5


@pytest.mark.parametrize(
    "events,msg",
    [
        ([], "missing progress telemetry"),
        (
            [{"seq": 2, "phase": "prefill"}, {"seq": 1, "phase": "prefill"}],
            "decreasing_sequence",
        ),
        (
            [
                {"seq": 1, "phase": "prefill", "processed_tokens": 2},
                {"seq": 2, "phase": "prefill", "processed_tokens": 1},
            ],
            "decreasing_processed",
        ),
        (
            [
                {
                    "seq": 1,
                    "phase": "prefill",
                    "processed_tokens": 2,
                    "total_prompt_tokens": 1,
                }
            ],
            "processed_exceeds_total",
        ),
        (
            [{"seq": 1, "phase": "completed"}, {"seq": 2, "phase": "prefill"}],
            "progress_after_terminal",
        ),
    ],
)
def test_progress_invariants_reject_bad_streams(events, msg):
    with pytest.raises(HarnessError, match=msg):
        validate_progress(events)


def test_metrics_throughput_and_budget():
    metrics = calculate_metrics(
        good_events(), actual_output_tokens=2, request_budget_seconds=10
    )
    assert metrics["durations_seconds"]["total"] == 5
    assert metrics["throughput"]["prompt_tokens_per_second"] == 50
    assert metrics["remaining_margin_seconds"] == 5


def test_kv_compare_boundaries_and_fallback_fail_closed():
    est = {"exact_kv_allocation_bytes": 1000, "conservative_fallback_used": False}
    assert (
        compare_kv_estimate(est, {"kv_allocation_bytes": 1003}, tolerance_bytes=4)[
            "pass"
        ]
        is True
    )
    assert (
        compare_kv_estimate(est, {"kv_allocation_bytes": 1005}, tolerance_bytes=4)[
            "pass"
        ]
        is False
    )
    with pytest.raises(HarnessError, match="exact_kv_comparison_unavailable"):
        compare_kv_estimate(
            {"conservative_fallback_used": True}, {"kv_allocation_bytes": 1000}
        )


def test_memory_adapter_platform_contracts_and_sanitization():
    assert validate_platform_backend("Darwin", "metal")["supported"] is True
    assert validate_platform_backend("Windows", "cuda")["supported"] is True
    assert validate_platform_backend("Plan9", "metal")["supported"] is False
    redacted = sanitize(
        {
            "path": "/Users/alice/private/model.gguf",
            "ciphertext": "abc",
            "msg": "A" * 600,
        }
    )
    assert "ciphertext" not in redacted
    assert "alice" not in redacted["path"]
    assert len(redacted["msg"]) == 512


def test_cancellation_and_recovery_fake_adapter_contract():
    adapter = FakePackagedAdapter(oracle_response(manifest()), good_events())
    prefill = adapter.cancel_at("prefill", 50)
    generation = adapter.cancel_at("generation", 1)
    for result in (prefill, generation):
        assert result["acknowledged"] is True
        assert result["terminal_state"] == "cancelled"
        assert result["cleanup_seconds"] < 20
        assert result["followup_succeeded"] is True
        assert result["operator_restart_succeeded"] is True


def test_atomic_report_schema_and_redaction(tmp_path: Path):
    path = atomic_write_report(
        {"prompt": "secret", "runtime": {"path": "/tmp/private/model.gguf"}}, tmp_path
    )
    payload = json.loads(path.read_text())
    assert payload["schema_version"].endswith("v1")
    assert "prompt" not in payload
    assert "private" not in json.dumps(payload)


def test_cli_generation_and_fail_closed_packaged(tmp_path: Path):
    assert (
        main(["generate-fixture", "--tier", "small-8k", "--out-dir", str(tmp_path)])
        == 0
    )
    assert (tmp_path / "small-8k.manifest.json").exists()
    with pytest.raises(SystemExit):
        main(["run-packaged", "--out-dir", str(tmp_path)])
    with pytest.raises(HarnessError, match="prerequisites"):
        main(
            [
                "run-packaged",
                "--app-binary",
                str(tmp_path / "missing"),
                "--model",
                str(tmp_path / "missing.gguf"),
                "--out-dir",
                str(tmp_path),
            ]
        )


def test_report_only_allows_semantic_failure_but_strict_fails(tmp_path: Path):
    fx = generate_fixture("small-8k")
    bad = json.dumps(
        {k: fx["manifest"]["expected"][k] for k in fx["manifest"]["required_keys"]}
        | {"XIV": "The Winged Monkeys"}
    )
    adapter = FakePackagedAdapter(bad, good_events())
    assert run_report_only(adapter, fx, strict=False, out_dir=tmp_path) == 0
    assert run_report_only(adapter, fx, strict=True, out_dir=tmp_path) == 1
