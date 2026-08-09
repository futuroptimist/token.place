import ast
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest

from scripts.long_context_benchmark import benchmark_harness as h

RUNNER_SOURCE = Path(__file__).parents[2] / "desktop-tauri/scripts/test_desktop_operator_ui_e2e.py"


@pytest.fixture
def desktop_runner():
    tree = ast.parse(RUNNER_SOURCE.read_text(encoding="utf-8"))
    names = {"_wait_for_packaged_setup_condition", "_prepare_packaged_landing_page",
        "_validate_packaged_failure_reason", "_enter_packaged_prompt",
        "_populate_and_submit_packaged_prompt"}
    functions = [node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ModuleType("desktop_runner_under_test")
    namespace = module.__dict__
    namespace.update({"webdriver": SimpleNamespace(Chrome=object), "ActionChains": object,
        "time": time, "By": SimpleNamespace(CSS_SELECTOR="css"),
        "Keys": SimpleNamespace(SHIFT="SHIFT", ENTER="ENTER"),
        "TimeoutException": TimeoutError, "RuntimeError": RuntimeError,
        "WebDriverWait": object,
        "PACKAGED_FAILURE_REASONS": h.PACKAGED_FAILURE_REASONS,
        "apply_benchmark_context_tier": h.apply_benchmark_context_tier})
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER_SOURCE), "exec"), namespace)
    return module

def _memory_evidence(*, baseline=100, peak=300, final=200, samples=3, platform="linux"):
    return {"method": h.MEMORY_METHOD, "scope": h.MEMORY_SCOPE, "platform": platform,
        "sample_count": samples, "baseline_rss_bytes": baseline,
        "peak_rss_bytes": peak, "final_rss_bytes": final}


def _runtime_configuration(backend="cpu", tier="64k-full", window=65536, qwen=False):
    na = {"status": "not_applicable", "reason": "not_qwen_64k_profile"}
    result = {"mode": {"requested": "cpu" if backend == "cpu" else "gpu",
            "effective": backend},
        "backend": {"requested": backend, "available": backend, "selected": backend,
            "used": backend, "fallback_reason": "none"},
        "context": {"tier": tier, "effective_window_tokens": window},
        "runtime_profile": dict(na), "batch_profile": dict(na), "kv_cache": dict(na),
        "acceleration": dict(na), "yarn_rope": dict(na)}
    if qwen:
        result.update({"runtime_profile": {
                "selected": "qwen64k_kv_q8_fa_balanced_batch",
                "preferred": "qwen64k_kv_q8_fa_balanced_batch",
                "attempted": ["qwen64k_kv_q8_fa_balanced_batch"], "recovery_count": 0,
                "result": "passed", "fallback_reason": "none"},
            "batch_profile": {"requested": "balanced", "selected": "balanced",
                "n_batch": 512, "n_ubatch": 128},
            "kv_cache": {"precision": "q8", "type_k": 8, "type_v": 8,
                "device": backend},
            "acceleration": {"flash_attention": True, "kqv_offload": True,
                "offloaded_layers": "all_supported_layers"},
            "yarn_rope": {"requested_context_tokens": 65536,
                "original_context_tokens": 32768, "context_multiplier": 2.0,
                "rope_frequency_scale": 0.5, "extension_factor_overridden": False,
                "scaling_source": "top_level_enum", "configuration_valid": True}})
    return result


def _qwen_kv_summary(backend="metal"):
    attestation = {"method":"active_runtime_selected_profile", "applicability":"qwen_64k_full",
        "architecture":"qwen3", "profile_id":"qwen64k_kv_q8_fa_balanced_batch",
        "backend":backend, "context_tier":"64k-full", "context_size_tokens":65536}
    return {"pass":True, "applicability":"qwen_64k_full",
        "profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":backend,
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "estimated_bytes":104857600, "observed_bytes":104857600, "delta_bytes":0,
        "precision_interval_bytes":[104852357, 104862843], "precision_bytes":5243,
        "record_count":1, "decimal_places":2,
        "estimator_provenance":"qwen_selected_profile_gguf_header",
        "runtime_provenance":"pinned_llama_cpp_kv_buffer_diagnostic", "attestation":attestation}


def _packaged_configuration_builder():
    source = (Path(__file__).parents[2] / "desktop-tauri" / "scripts" /
        "test_desktop_operator_ui_e2e.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {"_diagnostic_bool", "_diagnostic_int", "_diagnostic_float",
        "_normalize_profile_fallback_reason", "packaged_runtime_configuration"}
    nodes = [item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in names]
    namespace = {"__builtins__": __builtins__, "math": __import__("math")}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<packaged-configuration>", "exec"),
        namespace)
    return namespace["packaged_runtime_configuration"]


def _packaged_runtime_labels(backend="metal", *, tier="64k-full", window=65536):
    return {"Requested mode": "GPU" if backend != "cpu" else "CPU",
        "Effective mode": backend, "Backend available": backend,
        "Backend selected": backend, "Backend used": backend, "Fallback reason": "none",
        "Context tier": tier, "Context window": f"{window} tokens"}


def _qwen_readiness_diagnostics(*, result="passed", fallback="null"):
    return {"api_v1_readiness_qwen_64k_runtime_profile_id":
            "qwen64k_kv_q8_fa_balanced_batch",
        "api_v1_readiness_qwen_64k_runtime_preferred_profile_id":
            "qwen64k_kv_q8_fa_balanced_batch",
        "api_v1_readiness_qwen_64k_runtime_profile_attempt_ids":
            "qwen64k_kv_q8_fa_balanced_batch",
        "api_v1_readiness_qwen_64k_runtime_profile_recovery_count": "0",
        "api_v1_readiness_qwen_64k_runtime_profile_result": result,
        "api_v1_readiness_qwen_64k_runtime_profile_fallback_reason": fallback,
        "api_v1_readiness_qwen_64k_batch_profile_requested": "balanced",
        "api_v1_readiness_qwen_64k_batch_profile_selected": "balanced",
        "api_v1_readiness_qwen_64k_runtime_profile_n_batch": "512",
        "api_v1_readiness_qwen_64k_runtime_profile_n_ubatch": "128",
        "api_v1_readiness_qwen_64k_runtime_profile_kv_precision": "q8",
        "api_v1_readiness_qwen_64k_runtime_profile_type_k": "8",
        "api_v1_readiness_qwen_64k_runtime_profile_type_v": "8",
        "api_v1_readiness_qwen_64k_runtime_profile_flash_attn": "true",
        "api_v1_readiness_qwen_64k_runtime_profile_offload_kqv": "true",
        "kv_cache_device": "metal", "offloaded_layers": "all_supported_layers",
        "api_v1_readiness_yarn_requested_context_tokens": "65536",
        "api_v1_readiness_yarn_original_context_tokens": "32768",
        "api_v1_readiness_yarn_context_multiplier": "2.0",
        "api_v1_readiness_yarn_rope_freq_scale": "0.5",
        "api_v1_readiness_yarn_ext_factor_overridden": "false",
        "api_v1_readiness_yarn_rope_scaling_type_source": "top_level_enum",
        "api_v1_readiness_yarn_configuration_valid": "true"}


def test_desktop_runner_uses_evergreen_generation_settings_probe_name():
    source = (Path(__file__).parents[2] / "desktop-tauri" / "scripts" /
        "test_desktop_operator_ui_e2e.py").read_text(encoding="utf-8")
    assert "__p8" not in source
    assert source.count("__longContextBenchmarkGenerationSettings") == 3


def test_packaged_profile_fallback_normalizes_only_producer_absence_values():
    source = (Path(__file__).parents[2] / "desktop-tauri" / "scripts" /
        "test_desktop_operator_ui_e2e.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_normalize_profile_fallback_reason")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<fallback-normalizer>", "exec"),
        {"__builtins__": __builtins__}, namespace)
    normalize = namespace["_normalize_profile_fallback_reason"]
    assert [normalize(value) for value in (None, "", "null")] == ["none"] * 3
    assert normalize("capability_incompatibility") == "capability_incompatibility"
    assert normalize("arbitrary") == "arbitrary"


def test_matrix_plan_is_deterministic_complete_and_duplicate_free():
    first = h.build_matrix_plan()
    second = h.build_matrix_plan()
    assert first == second
    h.validate_matrix_plan(first)
    cells = first["cells"]
    assert len(cells) == 5 * 7
    assert len({h._canonical_json(cell) for cell in cells}) == len(cells)
    for platform_name, backend, package in h.MATRIX_PACKAGED_BACKENDS:
        scoped = [cell for cell in cells if (cell["platform"], cell["backend"], cell["package"])
            == (platform_name, backend, package)]
        assert sum(cell["trials"] for cell in scoped) == 18
        assert sum(cell["cancellation_sequences"] for cell in scoped) == 1
        assert {(cell["context_tier"], cell["fixture"], cell["scenario"])
            for cell in scoped if cell["trials"]} == {
                (tier, fixture, scenario) for tier, fixture in h.MATRIX_WORKLOADS
                for scenario in ("single-needle", "structured-extraction")}


def test_matrix_plan_entry_point_imports_without_os_killpg():
    script = Path(__file__).parents[2] / "scripts" / "long_context_benchmark.py"
    probe = textwrap.dedent(f"""
        import os
        import runpy
        import sys
        if hasattr(os, "killpg"):
            del os.killpg
        sys.argv = [{str(script)!r}, "matrix-plan"]
        try:
            runpy.run_path({str(script)!r}, run_name="__main__")
        except SystemExit as exc:
            if exc.code:
                raise
    """)
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    plan = json.loads(completed.stdout)
    h.validate_matrix_plan(plan)


def test_runtime_configuration_validation_is_exact_and_fail_closed():
    attestation = {"attestation": {"applicability": "not_applicable_verified_non_qwen",
        "architecture": "llama", "profile_id": "default"}}
    valid = _runtime_configuration()
    assert h.validate_runtime_configuration(valid, backend="cpu", context_tier="64k-full",
        context_tokens=65536, kv_attestation=attestation) == valid
    for mutation in (
            lambda item: item.update(secret="plaintext"),
            lambda item: item["context"].update(effective_window_tokens=True),
            lambda item: item["backend"].update(used="cuda"),
            lambda item: item.update(yarn_rope={"status": "not_applicable", "reason": "arbitrary"})):
        malformed = json.loads(json.dumps(valid)); mutation(malformed)
        with pytest.raises(ValueError, match="runtime_configuration_invalid"):
            h.validate_runtime_configuration(malformed, backend="cpu", context_tier="64k-full",
                context_tokens=65536, kv_attestation=attestation)


def test_qwen_runtime_configuration_requires_complete_valid_yarn_and_profile_evidence():
    attestation = {"type_k": "q8", "type_v": "q8", "attestation": {
        "applicability": "qwen_64k_full", "architecture": "qwen3",
        "profile_id": "qwen64k_kv_q8_fa_balanced_batch"}}
    valid = _runtime_configuration("metal", qwen=True)
    assert h.validate_runtime_configuration(valid, backend="metal", context_tier="64k-full",
        context_tokens=65536, kv_attestation=attestation) == valid
    for field, value in (("configuration_valid", False), ("rope_frequency_scale", float("nan")),
            ("requested_context_tokens", 65535)):
        malformed = json.loads(json.dumps(valid)); malformed["yarn_rope"][field] = value
        with pytest.raises(ValueError, match="runtime_configuration_invalid"):
            h.validate_runtime_configuration(malformed, backend="metal", context_tier="64k-full",
                context_tokens=65536, kv_attestation=attestation)


def test_runtime_configuration_binds_modes_applicability_and_p7_precision():
    summary = _qwen_kv_summary()
    valid = _runtime_configuration("metal", qwen=True)
    mutations = (
        ("mode", {"requested":"gpu", "effective":"gpu"}),
        ("runtime_profile", {**valid["runtime_profile"], "selected":"qwen64k_kv_q4_fa"}),
        ("kv_cache", {**valid["kv_cache"], "precision":"q4"}),
        ("kv_cache", {**valid["kv_cache"], "type_k":2}),
        ("context", {"tier":"64k-full", "effective_window_tokens":65535}),
        ("yarn_rope", {**valid["yarn_rope"], "rope_frequency_scale":1.0}),
    )
    for section, replacement in mutations:
        malformed = json.loads(json.dumps(valid)); malformed[section] = replacement
        with pytest.raises(ValueError, match="runtime_configuration_invalid"):
            h.validate_runtime_configuration(malformed, backend="metal", context_tier="64k-full",
                context_tokens=65536, kv_attestation=summary)
    for architecture, tier, window, reason in (
            ("llama", "64k-full", 65536, "not_applicable_verified_non_qwen"),
            ("qwen3", "8k-fast", 8192, "not_applicable_context_tier")):
        attestation = {"method":"active_runtime_selected_profile", "applicability":reason,
            "architecture":architecture, "profile_id":"default", "backend":"cpu",
            "context_tier":tier, "context_size_tokens":window}
        summary_na = {"pass":True, "applicability":reason, "reason":reason,
            "attestation":attestation}
        exact = _runtime_configuration("cpu", tier=tier, window=window)
        assert h.validate_runtime_configuration(exact, backend="cpu", context_tier=tier,
            context_tokens=window, kv_attestation=summary_na) == exact
        fabricated = json.loads(json.dumps(exact))
        fabricated["runtime_profile"] = valid["runtime_profile"]
        with pytest.raises(ValueError, match="runtime_configuration_invalid"):
            h.validate_runtime_configuration(fabricated, backend="cpu", context_tier=tier,
                context_tokens=window, kv_attestation=summary_na)


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


def test_fixture_target_prefixes_use_structural_value_anchors():
    prompt, manifest = h.generate_fixture("small-8k", scenario="structured-extraction")
    prompt_bytes = prompt.encode("utf-8")

    for key in ("VII", "XIV", "XXI"):
        cut = manifest["targets"][key]["target_prefix_utf8_bytes"]
        prefix = prompt_bytes[:cut].decode("utf-8")
        suffix = prompt_bytes[cut:].decode("utf-8")
        assert prefix.endswith(f"Chapter {key}: {h.STRUCTURED_HEADINGS[key]}\n")
        assert suffix.startswith(manifest["expected_answers"][key])
    vii_cut = manifest["targets"]["VII"]["target_prefix_utf8_bytes"]
    assert prompt_bytes[:vii_cut].decode("utf-8").endswith(
        "VII. They were obliged to camp out\n"
    )

    canary_cut = manifest["targets"]["canary"]["target_prefix_utf8_bytes"]
    assert prompt_bytes[:canary_cut].decode("utf-8").endswith("RECORD CANARY: ")


def test_single_needle_prefix_uses_record_anchor():
    prompt, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    cut = manifest["targets"]["needle"]["target_prefix_utf8_bytes"]
    assert prompt.encode("utf-8")[:cut].decode("utf-8").endswith("NEEDLE FACT: ")


def test_manifest_rejects_structured_heading_decoy_cut():
    prompt, manifest = h.generate_fixture("small-8k", scenario="structured-extraction")
    candidate = json.loads(json.dumps(manifest))
    heading_prefix = "Chapter VII: VII. "
    heading_cut = prompt.index(heading_prefix) + len(heading_prefix)
    candidate["targets"]["VII"]["target_prefix_utf8_bytes"] = heading_cut
    candidate["target_prefix_utf8_bytes"]["VII"] = heading_cut
    with pytest.raises(ValueError, match="manifest_target_prefix_invalid"):
        h.validate_manifest(candidate, prompt)


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
    est = {"profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal", "context_size_tokens":65536,
        "type_k":"q8", "type_v":"q8", "exact_kv_allocation_bytes":10000,
        "metadata_source":"gguf_header", "conservative_fallback_used":False}
    runtime = {"method":"pinned_llama_cpp_kv_buffer_diagnostic", "llama_cpp_python_version":"0.3.32",
        "llama_cpp_commit":"b3fed31b99f9bd37725833674252bccb429bb183", "observed_bytes":11000,
        "precision_bytes":5243, "record_count":1, "unit":"MiB", "decimal_places":2}
    assert h.compare_kv_estimate(est, runtime, backend="metal", context_tokens=65536)["pass"] is True
    runtime["observed_bytes"] = 16000
    assert h.compare_kv_estimate(est, runtime)["pass"] is False
    est["conservative_fallback_used"] = True
    assert h.compare_kv_estimate(est, runtime)["code"] == "kv_diagnostic_provenance_mismatch"


@pytest.mark.parametrize(("field", "value"), [
    ("profile_id", None), ("type_k", None), ("type_v", "unknown"),
    ("exact_kv_allocation_bytes", True), ("exact_kv_allocation_bytes", -1),
    ("exact_kv_allocation_bytes", 1 << 64),
])
def test_kv_compare_rejects_malformed_estimator_fields(field, value):
    estimate = {"profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "exact_kv_allocation_bytes":104857600, "metadata_source":"gguf_header",
        "conservative_fallback_used":False}
    runtime = {"method":"pinned_llama_cpp_kv_buffer_diagnostic",
        "llama_cpp_python_version":"0.3.32",
        "llama_cpp_commit":"b3fed31b99f9bd37725833674252bccb429bb183",
        "observed_bytes":104857600, "precision_bytes":5243, "record_count":1,
        "unit":"MiB", "decimal_places":2}
    estimate[field] = value
    assert h.compare_kv_estimate(estimate, runtime)["pass"] is False


@pytest.mark.parametrize(("field", "value"), [
    ("decimal_places", 10**9), ("record_count", 10**9),
])
def test_kv_compare_bounds_diagnostic_dimensions_before_arithmetic(field, value, monkeypatch):
    estimate = {"profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "exact_kv_allocation_bytes":104857600, "metadata_source":"gguf_header",
        "conservative_fallback_used":False}
    runtime = {"method":"pinned_llama_cpp_kv_buffer_diagnostic",
        "llama_cpp_python_version":"0.3.32",
        "llama_cpp_commit":"b3fed31b99f9bd37725833674252bccb429bb183",
        "observed_bytes":104857600, "precision_bytes":5243, "record_count":1,
        "unit":"MiB", "decimal_places":2}
    runtime[field] = value
    monkeypatch.setattr(h.math, "ceil", lambda _value: pytest.fail("arithmetic ran before bounds validation"))
    assert h.compare_kv_estimate(estimate, runtime)["pass"] is False


def test_kv_precision_arithmetic_and_report_shape_fail_closed():
    attestation = {"method":"active_runtime_selected_profile", "applicability":"qwen_64k_full",
        "architecture":"qwen3", "profile_id":"qwen64k_kv_q8_fa_balanced_batch",
        "backend":"metal", "context_tier":"64k-full", "context_size_tokens":65536}
    summary = {"pass":True, "applicability":"qwen_64k_full",
        "profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "estimated_bytes":104857600, "observed_bytes":104857600, "delta_bytes":0,
        "precision_interval_bytes":[104852357, 104862843], "precision_bytes":5243,
        "record_count":1, "decimal_places":2,
        "estimator_provenance":"qwen_selected_profile_gguf_header",
        "runtime_provenance":"pinned_llama_cpp_kv_buffer_diagnostic", "attestation":attestation}
    assert h.validate_kv_comparison_summary(summary)["pass"] is True
    for mutation in ({"precision_bytes":5242}, {"record_count":2},
            {"delta_bytes":1}, {"profile_id":None}, {"extra":True}):
        malformed = {**summary, **mutation}
        with pytest.raises(ValueError, match="report_kv_diagnostics_invalid"):
            h.validate_kv_comparison_summary(malformed)


def test_kv_applicability_is_profile_attested_not_filename_derived():
    qwen = {"method":"active_runtime_selected_profile", "applicability":"qwen_64k_full",
        "architecture":"qwen3", "profile_id":"qwen64k_kv_q8_fa_balanced_batch",
        "backend":"metal", "context_tier":"64k-full", "context_size_tokens":65536}
    assert h.validate_kv_applicability(qwen, backend="metal", context_tier="64k-full") == qwen
    non_qwen = {**qwen, "architecture":"llama", "profile_id":"default",
        "applicability":"not_applicable_verified_non_qwen"}
    assert h.validate_kv_applicability(non_qwen, backend="metal", context_tier="64k-full") == non_qwen
    with pytest.raises(ValueError, match="kv_applicability"):
        h.validate_kv_applicability(None, backend="metal", context_tier="64k-full")
    for context_size in (65535, 32768):
        with pytest.raises(ValueError, match="kv_applicability_context_mismatch"):
            h.validate_kv_applicability({**qwen, "context_size_tokens":context_size,
                "applicability":"not_applicable_context_tier"},
                backend="metal", context_tier="64k-full")
    non_64k = {**qwen, "context_tier":"8k-fast", "context_size_tokens":8192,
        "applicability":"not_applicable_context_tier"}
    assert h.validate_kv_applicability(non_64k, backend="metal", context_tier="8k-fast") == non_64k


def test_kv_report_summary_binds_profile_backend_context_and_attestation():
    qwen_attestation = {"method":"active_runtime_selected_profile", "applicability":"qwen_64k_full",
        "architecture":"qwen3", "profile_id":"qwen64k_kv_q8_fa_balanced_batch",
        "backend":"metal", "context_tier":"64k-full", "context_size_tokens":65536}
    summary = {"pass":True, "applicability":"qwen_64k_full",
        "profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
        "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
        "estimated_bytes":104857600, "observed_bytes":104857600, "delta_bytes":0,
        "precision_interval_bytes":[104852357, 104862843], "precision_bytes":5243,
        "record_count":1, "decimal_places":2,
        "estimator_provenance":"qwen_selected_profile_gguf_header",
        "runtime_provenance":"pinned_llama_cpp_kv_buffer_diagnostic", "attestation":qwen_attestation}
    assert h.validate_kv_comparison_summary(summary, backend="metal",
        context_tier="64k-full", context_tokens=65536)["pass"] is True
    for mutation, kwargs in (({"profile_id":"qwen64k_kv_q4_fa"}, {}),
            ({"backend":"cuda"}, {}), ({"estimated_bytes":1 << 63}, {}),
            ({}, {"context_tokens":8192}), ({}, {"context_tier":"8k-fast"})):
        with pytest.raises(ValueError, match="report_kv_diagnostics_invalid"):
            h.validate_kv_comparison_summary({**summary, **mutation}, backend="metal",
                context_tier=kwargs.get("context_tier", "64k-full"),
                context_tokens=kwargs.get("context_tokens", 65536))
    attestation = {"method":"active_runtime_selected_profile",
        "applicability":"not_applicable_verified_non_qwen", "architecture":"llama",
        "profile_id":"default", "backend":"metal", "context_tier":"64k-full",
        "context_size_tokens":65536}
    non_applicable = {"pass":True, "applicability":"not_applicable_verified_non_qwen",
        "reason":"not_applicable_verified_non_qwen", "attestation":attestation}
    assert h.validate_kv_comparison_summary(non_applicable, backend="metal",
        context_tier="64k-full", context_tokens=65536)["pass"] is True
    with pytest.raises(ValueError, match="report_kv_diagnostics_invalid"):
        h.validate_kv_comparison_summary({**non_applicable, "attestation":{**attestation,
            "architecture":"qwen3"}}, backend="metal", context_tier="64k-full",
            context_tokens=65536)


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
    proc = subprocess.run([sys.executable, "scripts/long_context_benchmark.py", "packaged-runtime", "--out-dir", str(tmp_path)], text=True, capture_output=True)
    assert proc.returncode == 2
    prompt, manifest = h.generate_fixture("small-8k")
    mf = tmp_path/"m.json"; mf.write_text(json.dumps(manifest))
    resp = tmp_path/"r.json"; resp.write_text(json.dumps(manifest["expected_answers"]))
    proc = subprocess.run([sys.executable, "scripts/long_context_benchmark.py", "evaluate", "--manifest", str(mf), "--response", str(resp), "--strict", "--out-dir", str(tmp_path)], text=True, capture_output=True)
    assert proc.returncode == 0


def test_platform_context_behavior():
    assert h.get_context_profile("8k-fast").total_context_tokens == 8192
    assert h.platform.system().lower() in {"linux", "darwin", "windows"}

def _physical_cancellation_evidence(total_prompt_tokens=100):
    def scenario(phase):
        return {"phase": phase, "trigger_observed": True, "trigger_count": 50,
            "threshold": 50, "total_prompt_tokens": total_prompt_tokens,
            "attempted": True, "acknowledged": True, "cleanup_s": 0.2,
            "quiescence_s": 0.5, "stale_progress_count": 0, "late_result_count": 0,
            "active_after_quiescence": False, "followup_ok": True, "followup_s": 1.0}
    return {"scenarios": [scenario("prefill"), scenario("generating")],
        "operator_lifecycle": {"stop_confirmed": True, "restart_ready": True,
            "session_changed": True, "restart_s": 2.0, "post_restart_followup_ok": True,
            "post_restart_followup_s": 1.0}}


def test_physical_cancellation_recovery_evidence_success_and_privacy():
    result = h.validate_cancellation_recovery(_physical_cancellation_evidence(),
        cleanup_budget_s=3, observation_window_s=0.5, recovery_timeout_s=3,
        total_prompt_tokens=100)
    assert result["pass"] is True
    serialized = json.dumps(result).lower()
    assert all(term not in serialized for term in ("request_id", "session_id",
        "response", "ciphertext", "credential", "cancel_token"))


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda v: v["scenarios"][0].update(trigger_observed=False), "cancellation_trigger_missed"),
    (lambda v: v["scenarios"][0].update(acknowledged=False), "cancellation_unconfirmed"),
    (lambda v: v["scenarios"][0].update(late_result_count=1), "cancellation_late_result"),
    (lambda v: v["scenarios"][0].update(stale_progress_count=1), "cancellation_stale_progress"),
    (lambda v: v["scenarios"][0].update(active_after_quiescence=True), "cancellation_stale_progress"),
    (lambda v: v["scenarios"][0].update(cleanup_s=4), "cancellation_cleanup_timeout"),
    (lambda v: v["scenarios"][0].update(followup_ok=False), "cancellation_followup_failed"),
    (lambda v: v["operator_lifecycle"].update(stop_confirmed=False), "operator_stop_failed"),
    (lambda v: v["operator_lifecycle"].update(session_changed=False), "operator_restart_failed"),
    (lambda v: v["operator_lifecycle"].update(restart_ready=False), "operator_restart_failed"),
    (lambda v: v["operator_lifecycle"].update(post_restart_followup_ok=False), "operator_followup_failed"),
    (lambda v: v["operator_lifecycle"].update(restart_s=4), "operator_restart_timeout"),
    (lambda v: v["scenarios"][0].pop("attempted"), "cancellation_evidence_malformed"),
])
def test_physical_cancellation_recovery_evidence_fails_closed(mutate, code):
    value = _physical_cancellation_evidence()
    mutate(value)
    with pytest.raises(ValueError, match=code):
        h.validate_cancellation_recovery(value, cleanup_budget_s=3,
            observation_window_s=0.5, recovery_timeout_s=3, total_prompt_tokens=100)


def test_physical_cancellation_threshold_mismatch_fails_closed():
    with pytest.raises(ValueError, match="cancellation_threshold_mismatched"):
        h.validate_cancellation_recovery(_physical_cancellation_evidence(),
            cleanup_budget_s=3, observation_window_s=0.5, recovery_timeout_s=3,
            total_prompt_tokens=100, prefill_threshold=49, generation_threshold=50)


@pytest.mark.parametrize(("count", "threshold", "total", "state"), [
    (50, 50, 100, "trigger"),
    (100, 50, 100, "completed"),
    (101, 50, 100, "completed"),
    (0, 1, 1, "completed"),
])
def test_prefill_cancellation_requires_interior_progress(count, threshold, total, state):
    assert h.prefill_cancellation_trigger_state(count, threshold, total) == state


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda value: value["scenarios"][0].pop("total_prompt_tokens"), "cancellation_evidence_malformed"),
    (lambda value: value["scenarios"][0].update(total_prompt_tokens="100"), "cancellation_evidence_malformed"),
    (lambda value: value["scenarios"][0].update(total_prompt_tokens=99), "cancellation_prompt_total_mismatched"),
    (lambda value: value["scenarios"][0].update(trigger_count=100), "cancellation_trigger_missed"),
    (lambda value: value["scenarios"][0].update(trigger_count=101), "cancellation_trigger_missed"),
])
def test_cancellation_prompt_total_evidence_fails_closed(mutate, code):
    value = _physical_cancellation_evidence()
    mutate(value)
    with pytest.raises(ValueError, match=code):
        h.validate_cancellation_recovery(value, cleanup_budget_s=3,
            observation_window_s=0.5, recovery_timeout_s=3, total_prompt_tokens=100)


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
    destination = tmp_path / "long_context_benchmark_report.json"
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


def test_owned_process_tree_memory_aggregation_handles_descendant_churn():
    class Process:
        def __init__(self, rss=0, children=(), error=None):
            self.rss, self.descendants, self.error = rss, list(children), error
        def children(self, recursive=False):
            assert recursive is True
            return self.descendants
        def memory_info(self):
            if self.error:
                raise self.error
            return type("Memory", (), {"rss": self.rss})()

    gone = Process(error=h.psutil.NoSuchProcess(9))
    denied = Process(error=h.psutil.AccessDenied(10))
    roots = iter([Process(100, [Process(40), gone]), Process(110, [Process(90), denied]),
        Process(80)])
    sampler = h.OwnedProcessTreeMemorySampler(7, lambda _pid: next(roots), system="Linux")
    assert [sampler.sample(), sampler.sample(), sampler.sample()] == [True, True, True]
    assert sampler.summary() == _memory_evidence(baseline=140, peak=200, final=80)


def test_owned_process_tree_memory_fails_without_valid_sample_or_platform():
    denied = lambda _pid: (_ for _ in ()).throw(h.psutil.AccessDenied(7))
    sampler = h.OwnedProcessTreeMemorySampler(7, denied, system="Linux")
    assert sampler.sample() is False
    with pytest.raises(ValueError, match="memory_sample_unavailable"):
        sampler.summary()
    assert h.normalized_memory_platform("Darwin") == "macos"
    assert h.normalized_memory_platform("Windows") == "windows"
    assert h.normalized_memory_platform("Plan9") == "unsupported"


@pytest.mark.parametrize("mutation", [
    lambda value: value.pop("scope"),
    lambda value: value.update(method="process_name_scan"),
    lambda value: value.update(platform="freebsd"),
    lambda value: value.update(sample_count=0),
    lambda value: value.update(peak_rss_bytes=99),
    lambda value: value.update(final_rss_bytes=-1),
    lambda value: value.update(pid=123),
])
def test_physical_memory_evidence_exact_shape_and_bounds(mutation):
    evidence = _memory_evidence()
    mutation(evidence)
    with pytest.raises(ValueError, match="physical_memory_evidence_invalid"):
        h.validate_physical_memory_evidence(evidence)


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
        "generation_settings": {"supplied": {"max_tokens": 1024},
            "omitted_runtime_default": ["seed", "temperature", "top_p"]},
        "messages": [{"content": "plaintext"}],
        "memory": _memory_evidence(),
        "runtime_configuration": _runtime_configuration("metal", qwen=True),
        "app_identity": "token.place-test",
        "runtime_identity": "bundled-test",
        "bundled_runtime_identity": "bundled-test",
        "build_identity": "unit-test",
        "backend_requested": "metal", "backend_selected": "metal", "backend_used": "metal",
        "model_fingerprint": "sha256:test",
        "authoritative_prompt_tokens": authoritative_total,
        "authoritative_tokenizer_evidence": {"method": "packaged_admission_render_and_tokenize_chat", "runtime_identity": "bundled-test", "fixture_sha256": manifest["fixture_sha256"], "total_prompt_tokens": authoritative_total, "target_offsets_tokens": authoritative_offsets},
        "kv_applicability": {"method":"active_runtime_selected_profile",
            "applicability":"qwen_64k_full", "architecture":"qwen3",
            "profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal", "context_tier":"64k-full",
            "context_size_tokens":65536},
        "kv_estimate":{"profile_id":"qwen64k_kv_q8_fa_balanced_batch", "backend":"metal",
            "context_size_tokens":65536, "type_k":"q8", "type_v":"q8",
            "exact_kv_allocation_bytes":104857600, "metadata_source":"gguf_header",
            "conservative_fallback_used":False},
        "kv_runtime":{"method":"pinned_llama_cpp_kv_buffer_diagnostic",
            "llama_cpp_python_version":"0.3.32",
            "llama_cpp_commit":"b3fed31b99f9bd37725833674252bccb429bb183",
            "observed_bytes":104857600, "precision_bytes":5243, "record_count":1,
            "unit":"MiB", "decimal_places":2},
        "cancellation_recovery": _physical_cancellation_evidence(authoritative_total),
    }


    app = tmp_path / "app"; app.write_text("app"); app.chmod(0o700)
    payload["cancellation_recovery"]["scenarios"][0].update(
        threshold=max(1, int(authoritative_total * 0.5)),
        trigger_count=max(1, int(authoritative_total * 0.5)))
    payload["cancellation_recovery"]["scenarios"][1].update(threshold=8, trigger_count=8)
    seen = {}
    def fake_run(command, **kwargs):
        seen.update(command=command, kwargs=kwargs)
        request_path = command[command.index("--benchmark-request") + 1]
        evidence_path = command[command.index("--benchmark-evidence") + 1]
        seen["request"] = json.loads(h.Path(request_path).read_text())
        h.Path(evidence_path).write_text(json.dumps(payload))
        return subprocess.CompletedProcess(command, 0, "", "")

    result = h.invoke_packaged_runtime_adapter(timeout_s=3.0, app_binary=str(app), model=str(model),
        backend="metal", relay_url="https://relay.example", cleanup_timeout_s=3.0,
        external_prompt=prompt, external_manifest=manifest, subprocess_run=fake_run,
        cancellation_validation=True, prefill_cancel_fraction=0.5,
        generation_cancel_tokens=8, observation_window_s=0.5, recovery_timeout_s=3)
    assert seen["request"]["fixture_id"] == "small-8k"
    assert seen["request"]["prompt"] not in json.dumps(result)
    assert result["runner_kind"] == "repository_packaged_desktop_webdriver"
    assert result["pass"] is True
    assert result["fixture"]["estimated_prompt_tokens"] != result["fixture"]["authoritative_prompt_tokens"]
    assert result["fixture"]["authoritative_target_offsets_tokens"] == authoritative_offsets
    assert seen["request"]["cancellation_validation"] is True
    assert result["cancellation_recovery"]["pass"] is True
    assert result["memory"] == _memory_evidence()
    assert "messages" not in result
    assert not h.Path(seen["command"][seen["command"].index("--benchmark-request") + 1]).exists()
    assert not h.Path(seen["command"][seen["command"].index("--benchmark-evidence") + 1]).exists()

    payload["cancellation_recovery"]["scenarios"][0]["trigger_count"] = authoritative_total
    failed = h.invoke_packaged_runtime_adapter(timeout_s=3.0, app_binary=str(app), model=str(model),
        backend="metal", relay_url="https://relay.example", cleanup_timeout_s=3.0,
        external_prompt=prompt, external_manifest=manifest, subprocess_run=fake_run,
        cancellation_validation=True, prefill_cancel_fraction=0.5,
        generation_cancel_tokens=8, observation_window_s=0.5, recovery_timeout_s=3,
        report_only=True)
    assert failed["code"] == "cancellation_trigger_missed"
    assert failed["runtime_contract_pass"] is False


def test_packaged_runtime_external_fixture_pair_and_hash_fail_closed(tmp_path):
    prompt, manifest = h.generate_fixture("small-8k")
    model = tmp_path / "qwen-in-name-but-verified-llama.gguf"; model.write_bytes(b"x")
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
            phase_path = command[command.index("--benchmark-phase-status") + 1]
            _write_phase(h.Path(phase_path), "request_active", 0.0)
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if runner_outcome == "failed":
            phase_path = command[command.index("--benchmark-phase-status") + 1]
            _write_phase(h.Path(phase_path), "cleanup", 0.0,
                last_safe_phase="landing_page_ready", failure_reason="send_button_not_enabled",
                cleanup_succeeded=True)
            return subprocess.CompletedProcess(command, 1, "", "")
        evidence_path = command[command.index("--benchmark-evidence") + 1]
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
    if runner_outcome == "timeout":
        assert result["last_safe_phase"] == "request_active"
        assert result["request_timeout_s"] == 1
        assert result["runner_timeout_s"] == (
            h.PACKAGED_SETUP_BUDGET_S + 1 + h.PACKAGED_FINALIZATION_BUDGET_S)
        assert result["overall_timeout_s"] == result["runner_timeout_s"] + 1
        assert result["cleanup_succeeded"] is False


@pytest.mark.parametrize(("contents", "expected"), [
    (None, "packaged_phase_status_missing"),
    ("not-json", "packaged_phase_status_missing"),
    (json.dumps({"schema_version": "wrong", "phase": "request_active",
        "sequence": 6, "elapsed_s": 0}), "packaged_phase_status_malformed"),
    (json.dumps({"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": "request_active", "sequence": 6, "elapsed_s": 50}),
        "packaged_phase_status_malformed"),
    (json.dumps({"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": "request_active", "sequence": 6, "last_safe_phase": "operator_ready",
        "failure_reason": [], "elapsed_s": 0, "cleanup_succeeded": None}),
        "packaged_phase_status_malformed"),
    (json.dumps({"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": "request_active", "sequence": 6, "last_safe_phase": "operator_ready",
        "failure_reason": None, "elapsed_s": 0, "cleanup_succeeded": {}}),
        "packaged_phase_status_malformed"),
])
def test_packaged_phase_status_missing_malformed_or_stale_fails_closed(tmp_path, contents, expected):
    path = tmp_path / "phase.json"
    if contents is not None:
        path.write_text(contents)
    assert h._read_packaged_phase_status(path, 1) == (None, expected)


def test_packaged_adapter_watchdog_is_explicit_and_cli_compatible(tmp_path):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    observed = {}
    def fake_run(command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        request_path = command[command.index("--benchmark-request") + 1]
        observed["request"] = json.loads(h.Path(request_path).read_text())
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, failure_reason="packaged_runner_failure", cleanup_succeeded=True)
        return subprocess.CompletedProcess(command, 1)
    result = h.invoke_packaged_runtime_adapter(timeout_s=600, app_binary=str(app),
        model=str(model), backend="cuda", relay_url="https://relay.example",
        cleanup_timeout_s=30, subprocess_run=fake_run)
    assert result["code"] == "packaged_runner_failed"
    work_budget = h.PACKAGED_SETUP_BUDGET_S + 600 + h.PACKAGED_FINALIZATION_BUDGET_S
    assert observed["timeout"] == work_budget + 30
    assert observed["request"]["phase_status_version"] == h.PACKAGED_PHASE_STATUS_VERSION
    assert observed["request"]["phase_status_phases"] == list(h.PACKAGED_PHASES)
    assert observed["request"]["request_timeout_s"] == 600
    assert observed["request"]["setup_timeout_s"] == h.PACKAGED_SETUP_BUDGET_S
    assert observed["request"]["finalization_timeout_s"] == h.PACKAGED_FINALIZATION_BUDGET_S
    assert observed["request"]["cancellation_timeout_s"] == 0


def test_cancellation_budget_is_named_additive_and_bounded(tmp_path):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    observed = {}
    def fake_run(command, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        request_path = command[command.index("--benchmark-request") + 1]
        observed["request"] = json.loads(h.Path(request_path).read_text())
        return subprocess.CompletedProcess(command, 1)
    h.invoke_packaged_runtime_adapter(timeout_s=10, app_binary=str(app), model=str(model),
        backend="cuda", relay_url="https://relay.example", cleanup_timeout_s=3,
        cancellation_validation=True, prefill_cancel_fraction=0.5,
        observation_window_s=2, recovery_timeout_s=4, subprocess_run=fake_run)
    cancellation = h.packaged_cancellation_budget_s(10, 2, 4)
    assert cancellation == 56
    assert observed["request"]["cancellation_timeout_s"] == cancellation
    assert observed["timeout"] == (h.PACKAGED_SETUP_BUDGET_S + 10
        + h.PACKAGED_FINALIZATION_BUDGET_S + cancellation + 3)


def test_cancellation_budget_enumerates_every_bounded_operation():
    request, observation, recovery = 11, 3, 5
    bounded_operations = ([request] * 2 + [observation] * 2 + [recovery] * 8)
    assert h.packaged_cancellation_budget_s(request, observation, recovery) == sum(bounded_operations)


def test_cancellation_phase_and_finalization_allowances_are_independent():
    now = [10.0]
    def consume_complete_cancellation_allowance():
        now[0] += 56.0
        return "validated"
    result, finalization_deadline = h.start_phase_after(
        consume_complete_cancellation_allowance, 120.0, clock=lambda: now[0])
    assert result == "validated"
    assert finalization_deadline == 186.0
    assert h.packaged_phase_remaining(finalization_deadline, "timeout",
        clock=lambda: now[0]) == 120.0


def test_cancellation_deadline_exhaustion_fails_closed_with_fake_clock():
    with pytest.raises(RuntimeError, match="packaged cancellation validation timeout"):
        h.packaged_phase_remaining(1.0, "packaged cancellation validation timeout",
            clock=lambda: 1.0)


def test_disabled_cancellation_has_no_budget_or_cli_contract_change(tmp_path):
    model = tmp_path / "model.gguf"; model.write_bytes(b"x")
    app = tmp_path / "app"; app.write_text("x"); app.chmod(0o700)
    observed = {}
    def fake_run(command, **kwargs):
        request_path = command[command.index("--benchmark-request") + 1]
        observed.update(json.loads(h.Path(request_path).read_text()))
        return subprocess.CompletedProcess(command, 1)
    h.invoke_packaged_runtime_adapter(timeout_s=10, app_binary=str(app), model=str(model),
        backend="cuda", relay_url="https://relay.example", cleanup_timeout_s=3,
        subprocess_run=fake_run)
    assert observed["cancellation_timeout_s"] == 0
    assert observed["cancellation_validation"] is False


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
        sys.executable, "scripts/long_context_benchmark.py", "packaged-runtime",
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
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, failure_reason="packaged_runner_failure", cleanup_succeeded=True)
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
        "generation_settings":{"supplied":{"max_tokens":1024},
            "omitted_runtime_default":["seed", "temperature", "top_p"]},
        "memory": _memory_evidence(),
        "runtime_configuration": _runtime_configuration(),
        "prefill_end_s": 1.0, "first_token_s": 1.0, "end_s": 2.0, "output_tokens": 4,
        "result_observation":{"kind":"result", "status":"success", "sequence":3, "elapsed_ms":2001},
        "terminal_observation":{"kind":"terminal", "state":"completed", "sequence":4, "elapsed_ms":2002},
        "post_terminal_observations":[],
        "app_identity": "token.place", "runtime_identity": "bundled",
        "bundled_runtime_identity": "bundled", "build_identity": "build",
        "backend_requested": "cpu", "backend_selected": "cpu", "backend_used": "cpu", "model_fingerprint": "sha256:test",
        "authoritative_prompt_tokens": manifest["actual_tokens"],
        "authoritative_tokenizer_evidence": {"method": "packaged_admission_render_and_tokenize_chat", "runtime_identity": "bundled", "fixture_sha256": manifest["fixture_sha256"], "total_prompt_tokens": manifest["actual_tokens"], "target_offsets_tokens": {key: value["actual_offset_tokens"] for key, value in manifest["targets"].items()}},
        "kv_applicability": {"method":"active_runtime_selected_profile",
            "applicability":"not_applicable_verified_non_qwen", "architecture":"llama",
            "profile_id":"default", "backend":"cpu", "context_tier":"64k-full",
            "context_size_tokens":65536},
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
        h.Path(command[command.index("--benchmark-evidence") + 1]).write_text(json.dumps(payload))
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
        _write_phase(h.Path(command[command.index("--benchmark-phase-status") + 1]),
            "cleanup", 0.0, failure_reason="packaged_runner_failure", cleanup_succeeded=True)
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
    assert h.classify_benchmark_landing_state(state) == expected


def test_desktop_runner_applies_tier_and_maps_operator_mode():
    class Browser:
        def execute_script(self, script, tier):
            assert "selectedContextTier" in script
            self.tier = tier
            return tier
    browser = Browser()
    assert h.apply_benchmark_context_tier(browser, "64k-full") == "64k-full"
    assert browser.tier == "64k-full"
    assert h.benchmark_operator_mode("cpu") == "cpu"
    assert h.benchmark_operator_mode("metal") == "gpu"
    assert h.benchmark_operator_mode("cuda") == "gpu"
    with pytest.raises(ValueError):
        h.benchmark_operator_mode("mock")


def test_packaged_multiline_prompt_uses_shift_enter_and_submits_after_exact_population(
        desktop_runner, monkeypatch):
    events = []
    prompt = "line1\nline2\n\nline4"
    class Field:
        parent = object()
        def send_keys(self, value): events.append(("text", value))
    class Button:
        def is_enabled(self): events.append(("eligibility",)); return True
        def click(self): events.append(("click",))
    field, button = Field(), Button()
    class Browser:
        def find_element(self, _by, selector):
            return field if selector == ".message-input" else button
        def execute_script(self, _script): events.append(("population",)); return prompt
    class Actions:
        def __init__(self, _parent): pass
        def key_down(self, key): events.append(("key_down", key)); return self
        def send_keys(self, key): events.append(("newline", key)); return self
        def key_up(self, key): events.append(("key_up", key)); return self
        def perform(self): events.append(("perform",)); return self
    class Wait:
        def __init__(self, browser, timeout, **_kwargs): pass
        def until(self, predicate): return predicate(Browser())
    monkeypatch.setattr(desktop_runner, "WebDriverWait", Wait)
    phases = []
    def checkpoint(phase):
        phases.append(phase)
        events.append(("phase", phase))
    started = desktop_runner._populate_and_submit_packaged_prompt(
        Browser(), prompt, lambda: 10, pytest.fail, checkpoint,
        clock=lambda: events.append(("timer",)) or 42.0, action_factory=Actions)
    assert started == 42.0
    assert [event for event in events if event[0] == "text"] == [
        ("text", "line1"), ("text", "line2"), ("text", "line4")]
    assert len([event for event in events if event[0] == "newline"]) == 3
    assert all(event[1] == desktop_runner.Keys.ENTER
        for event in events if event[0] == "newline")
    assert not any(event == ("text", desktop_runner.Keys.ENTER) for event in events)
    assert events.index(("population",)) < events.index(("eligibility",))
    assert events.index(("timer",)) + 1 == events.index(("click",))
    assert phases == ["landing_page_ready", "request_active"]
    assert events.index(("click",)) < events.index(("phase", "request_active"))


def test_packaged_prompt_fails_closed_when_setup_expires_before_click(
        desktop_runner, monkeypatch):
    events = []
    prompt = "ready"
    class Field:
        parent = object()
        def send_keys(self, value): events.append(("text", value))
    class Button:
        def is_enabled(self): return True
        def click(self): events.append(("click",))
    field, button = Field(), Button()
    class Browser:
        def find_element(self, _by, selector):
            return field if selector == ".message-input" else button
        def execute_script(self, _script): return prompt
    class Wait:
        def __init__(self, browser, timeout, **_kwargs): pass
        def until(self, predicate): return predicate(Browser())
    monkeypatch.setattr(desktop_runner, "WebDriverWait", Wait)
    remaining_calls = 0
    def setup_remaining():
        nonlocal remaining_calls
        remaining_calls += 1
        if remaining_calls == 5:
            raise RuntimeError("packaged setup timeout")
        return 10
    def checkpoint(phase): events.append(("phase", phase))
    with pytest.raises(RuntimeError, match="packaged setup timeout"):
        desktop_runner._populate_and_submit_packaged_prompt(
            Browser(), prompt, setup_remaining, pytest.fail, checkpoint,
            clock=lambda: events.append(("timer",)) or 42.0)
    assert remaining_calls == 5
    assert ("phase", "landing_page_ready") in events
    assert ("timer",) not in events
    assert ("click",) not in events
    assert ("phase", "request_active") not in events


def test_packaged_prompt_rejects_inexact_vue_population(desktop_runner):
    class Field:
        parent = object()

        def send_keys(self, _value):
            pass

    class Browser:
        def find_element(self, _by, _selector):
            return Field()

        def execute_script(self, _script):
            return "partial prompt"

    failures = []

    def fail_closed(reason):
        failures.append(reason)
        raise RuntimeError(reason)

    with pytest.raises(RuntimeError, match="message_input_not_populated"):
        desktop_runner._populate_and_submit_packaged_prompt(
            Browser(), "complete prompt", lambda: 10, fail_closed, pytest.fail)
    assert failures == ["message_input_not_populated"]


def test_packaged_runner_setup_timeout_records_sanitized_cleanup_checkpoint(tmp_path):
    """Exercise the real runner's pre-launch failure and final checkpoint path."""
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_write_benchmark_phase", "run_long_context_packaged_mode"}
    functions = [node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {
        "Path": Path, "json": json, "time": time, "tempfile": __import__("tempfile"),
        "os": os, "shutil": __import__("shutil"), "contextlib": __import__("contextlib"),
        "PACKAGED_FAILURE_REASONS": h.PACKAGED_FAILURE_REASONS,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(RUNNER_SOURCE), "exec"),
        namespace)
    request_path = tmp_path / "request.json"
    phase_path = tmp_path / "phase.json"
    request_path.write_text(json.dumps({
        "phase_status_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase_status_phases": list(h.PACKAGED_PHASES),
        "setup_timeout_s": 0,
        "cleanup_timeout_s": 1,
        "manifest": {"fixture_sha256": "0" * 64, "targets": {}},
    }))

    with pytest.raises(RuntimeError, match="packaged setup timeout"):
        namespace["run_long_context_packaged_mode"](
            request_path, tmp_path / "evidence.json", phase_path, tmp_path / "app")

    checkpoint = json.loads(phase_path.read_text())
    assert checkpoint["phase"] == "cleanup"
    assert checkpoint["failure_reason"] == "packaged_runner_failure"
    assert checkpoint["cleanup_succeeded"] is True


@pytest.mark.parametrize("reason", [
    "vue_not_ready", "client_keypair_not_ready", "model_selection_not_ready",
    "send_button_not_enabled",
])
def test_packaged_setup_exhaustion_preserves_specific_failure_reason(desktop_runner, reason):
    observed = []
    def exhausted():
        raise RuntimeError("packaged setup timeout")
    def fail_closed(value):
        observed.append(value)
        raise LookupError(value)
    with pytest.raises(LookupError, match=reason):
        desktop_runner._wait_for_packaged_setup_condition(
            object(), exhausted, lambda _browser: True, reason, fail_closed)
    assert observed == [reason]


def test_packaged_landing_page_readiness_checks_and_context_are_bounded(
        desktop_runner, monkeypatch):
    scripts = []
    remaining_calls = []

    class Browser:
        def execute_script(self, script, *_args):
            scripts.append(script)
            return "64k-full" if "selectedContextTier" in script else True

    class Wait:
        def __init__(self, browser, timeout, **_kwargs):
            self.browser = browser
            assert timeout == 10

        def until(self, predicate):
            return predicate(self.browser)

    monkeypatch.setattr(desktop_runner, "WebDriverWait", Wait)

    def setup_remaining():
        remaining_calls.append(True)
        return 10

    desktop_runner._prepare_packaged_landing_page(
        Browser(), setup_remaining, pytest.fail, "64k-full")
    assert len(remaining_calls) == 4
    assert any("hasClientKeypair" in script for script in scripts)
    assert any("modelsLoaded" in script for script in scripts)


def test_packaged_landing_page_rejects_wrong_context_tier(desktop_runner, monkeypatch):
    class Browser:
        def execute_script(self, script, *_args):
            return "8k-fast" if "selectedContextTier" in script else True

    class Wait:
        def __init__(self, browser, _timeout, **_kwargs):
            self.browser = browser

        def until(self, predicate):
            return predicate(self.browser)

    monkeypatch.setattr(desktop_runner, "WebDriverWait", Wait)
    with pytest.raises(RuntimeError, match="requested_context_tier_not_applied"):
        desktop_runner._prepare_packaged_landing_page(
            Browser(), lambda: 10,
            lambda reason: (_ for _ in ()).throw(RuntimeError(reason)), "64k-full")


def test_packaged_runner_never_bypasses_production_send_eligibility():
    source = RUNNER_SOURCE.read_text(encoding="utf-8")
    benchmark = source[source.index("def run_long_context_packaged_mode"):source.index(
        "def _long_context_followup_request")]
    assert "sendMessage(" not in benchmark
    assert "disabled = false" not in benchmark
    assert "removeAttribute('disabled')" not in benchmark
    assert '_populate_and_submit_packaged_prompt(browser, request["prompt"]' in benchmark


def test_packaged_failure_reasons_are_explicit_low_cardinality_categories():
    assert {"vue_not_ready", "client_keypair_not_ready", "model_selection_not_ready",
        "requested_context_tier_not_applied", "message_input_not_populated",
        "send_button_not_enabled"}.issubset(h.PACKAGED_FAILURE_REASONS)
    assert all(value.isascii() and value.replace("_", "").islower()
        for value in h.PACKAGED_FAILURE_REASONS)


@pytest.mark.parametrize("reason", sorted(h.PACKAGED_FAILURE_REASONS))
def test_desktop_runner_accepts_every_packaged_failure_reason(desktop_runner, reason):
    assert desktop_runner._validate_packaged_failure_reason(reason) == reason


def test_desktop_runner_rejects_unlisted_packaged_failure_reason(desktop_runner):
    with pytest.raises(RuntimeError, match="invalid packaged failure reason"):
        desktop_runner._validate_packaged_failure_reason("prompt content")


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
            time.sleep(timeout)
            raise subprocess.TimeoutExpired("runner", timeout)
        return outcome

    def kill(self):
        self.killed = True


def _write_phase(path, phase, elapsed, *, last_safe_phase=None, failure_reason=None,
        cleanup_succeeded=None):
    path.write_text(json.dumps({"schema_version": h.PACKAGED_PHASE_STATUS_VERSION,
        "phase": phase, "sequence": h.PACKAGED_PHASES.index(phase) + 1,
        "last_safe_phase": last_safe_phase or phase, "failure_reason": failure_reason,
        "elapsed_s": elapsed, "cleanup_succeeded": cleanup_succeeded}))


def test_owned_runner_allows_on_time_child_cleanup_once(tmp_path):
    clock = [0.0]
    phase = tmp_path / "phase.json"
    class Process(_TimedOutProcess):
        def wait(self, timeout):
            clock[0] += timeout
            if clock[0] >= 2.0 and not phase.exists():
                _write_phase(phase, "cleanup", 2.0)
            if clock[0] < 4.0:
                raise subprocess.TimeoutExpired("runner", timeout)
            return 0
    process = Process([])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    completed = h._run_owned_runner(["runner"], 10, 5,
        popen=lambda _command, **_kwargs: process, phase_status_path=phase,
        platform_name="posix", clock=lambda: clock[0])
    assert completed.returncode == 0
    assert 4.0 <= clock[0] < 5.1
    assert process.killed is False


def test_owned_runner_work_timeout_does_not_borrow_cleanup_window(tmp_path):
    clock = [0.0]
    phase = tmp_path / "phase.json"
    _write_phase(phase, "request_active", 9.5)
    class Process(_TimedOutProcess):
        def wait(self, timeout):
            if process.killed:
                return 0
            clock[0] += timeout
            raise subprocess.TimeoutExpired("runner", timeout)
    process = Process([])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    signals = []
    def kill_group(pid, sig):
        signals.append(sig)
        if sig == signal.SIGTERM:
            process.killed = True
        if sig == 0:
            raise ProcessLookupError
    with pytest.raises(h.PackagedRunnerTimeout) as raised:
        h._run_owned_runner(["runner"], 10, 5,
            popen=lambda _command, **_kwargs: process, phase_status_path=phase,
            killpg=kill_group, platform_name="posix", clock=lambda: clock[0])
    assert raised.value.timeout == 10
    assert signal.SIGTERM in signals
    assert raised.value.cleanup_succeeded is True


def test_owned_runner_cleanup_overrun_is_bounded_and_fails_closed(tmp_path):
    clock = [0.0]
    phase = tmp_path / "phase.json"
    class Process(_TimedOutProcess):
        def wait(self, timeout):
            clock[0] += timeout
            if clock[0] >= 2.0 and not phase.exists():
                _write_phase(phase, "cleanup", 2.0)
            raise subprocess.TimeoutExpired("runner", timeout)
    process = Process([])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    signals = []
    with pytest.raises(h.PackagedRunnerTimeout) as raised:
        h._run_owned_runner(["runner"], 10, 5,
            popen=lambda _command, **_kwargs: process, phase_status_path=phase,
            killpg=lambda _pid, sig: signals.append(sig), platform_name="posix",
            clock=lambda: clock[0])
    assert 5.0 <= clock[0] < 10.0
    assert signal.SIGKILL in signals
    assert raised.value.cleanup_succeeded is False


def test_owned_runner_posix_terminates_exact_process_group(monkeypatch):
    process = _TimedOutProcess(["timeout", "timeout", -9])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    launched = {}
    signals = []
    def kill_group(pid, sig):
        signals.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda command, **kwargs: launched.update(kwargs) or process,
            killpg=kill_group, platform_name="posix", phase_poll_interval_s=10)
    assert launched["start_new_session"] is True
    assert signals == [(731, signal.SIGTERM), (731, signal.SIGKILL),
        (731, signal.SIGKILL)]
    assert raised.value.cleanup_succeeded is False


def test_owned_runner_posix_does_not_claim_cleanup_while_group_survives(monkeypatch):
    clock = [0.0]
    class Process(_TimedOutProcess):
        def wait(self, timeout):
            if self.killed:
                return 0
            clock[0] += timeout
            raise subprocess.TimeoutExpired("runner", timeout)
    process = Process([])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    monkeypatch.setattr(h.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def surviving_group(_pid, sig):
        if sig == signal.SIGTERM:
            process.killed = True
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda _command, **_kwargs: process,
            killpg=surviving_group, platform_name="posix", clock=lambda: clock[0])
    assert raised.value.cleanup_succeeded is False


@pytest.mark.parametrize("cleanup_outcome", ["failed", "timeout"])
def test_owned_runner_windows_never_invokes_injected_killpg(cleanup_outcome):
    process = _TimedOutProcess(["timeout", 1] if cleanup_outcome == "failed" else ["timeout", 1])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    launched = {}; cleanup = []
    def cleanup_run(command, **kwargs):
        cleanup.append((command, kwargs))
        if cleanup_outcome == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 1)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda command, **kwargs: launched.update(kwargs) or process,
            cleanup_run=cleanup_run,
            killpg=lambda *_args: pytest.fail("Windows cleanup called POSIX killpg"),
            platform_name="nt", phase_poll_interval_s=10)
    assert launched["creationflags"] == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert cleanup[0][0] == ["taskkill", "/PID", "731", "/T", "/F"]
    assert process.killed is True
    assert raised.value.cleanup_succeeded is False


def test_owned_runner_windows_never_resolves_os_killpg(monkeypatch):
    process = _TimedOutProcess(["timeout", 1])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    cleanup = []
    monkeypatch.delattr(os, "killpg", raising=False)

    with pytest.raises(subprocess.TimeoutExpired):
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda _command, **_kwargs: process,
            cleanup_run=lambda command, **kwargs: cleanup.append((command, kwargs))
            or subprocess.CompletedProcess(command, 0),
            platform_name="nt", phase_poll_interval_s=10)

    assert cleanup[0][0] == ["taskkill", "/PID", "731", "/T", "/F"]


def test_owned_runner_posix_fails_closed_without_killpg(monkeypatch):
    process = _TimedOutProcess(["timeout", 1])
    process.stdout = type("Output", (), {"read": lambda self, size: b""})()
    monkeypatch.delattr(os, "killpg", raising=False)
    with pytest.raises(RuntimeError, match="^owned_process_group_cleanup_unavailable$"):
        h._run_owned_runner(["runner"], 1, 2,
            popen=lambda _command, **_kwargs: process, platform_name="posix",
            phase_poll_interval_s=10)
    assert process.killed is True


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
    assert json.loads((report_dir / "long_context_benchmark_report.json").read_text())["semantic"]["semantic_pass"]


def test_bounded_external_fixture_reader_rejects_oversized_utf8(tmp_path):
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("éé", encoding="utf-8")

    assert h._read_bounded_text(str(fixture), limit=4) == "éé"
    with pytest.raises(ValueError, match="fixture_too_large"):
        h._read_bounded_text(str(fixture), limit=3)


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
        "semantic":{"semantic_pass":False, "exact_match":False, "errors":["exact_match"]},
        "generation_settings":{"supplied":{"max_tokens":1024},
            "omitted_runtime_default":["seed", "temperature", "top_p"]},
        "memory": _memory_evidence(),
        "runtime_configuration": _runtime_configuration(),
        "kv_compare":{"pass":True, "applicability":"not_applicable_verified_non_qwen",
            "reason":"not_applicable_verified_non_qwen",
            "attestation":{"method":"active_runtime_selected_profile",
                "applicability":"not_applicable_verified_non_qwen", "architecture":"llama",
                "profile_id":"default", "backend":"cpu", "context_tier":"64k-full",
                "context_size_tokens":65536}}}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **kwargs: evidence)
    args = ["packaged-runtime", "--out-dir", str(tmp_path), "--app-binary", "app",
        "--model", "model", "--backend", "cpu", "--relay-url", "https://relay.example",
        "--report-only"]
    assert h.main(args) == 0
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["overall_pass"] is False
    assert report["semantic"]["semantic_pass"] is False
    assert report["report_only_accepted"] is True

    evidence["report_only_accepted"] = False
    assert h.main(args) == 0


def _packaged_main_evidence(semantic_pass=True, *, max_tokens=1024):
    return {"pass": semantic_pass, "report_only_accepted": False, "runtime_contract_pass": True,
        "fixture":{"sha256":"abc", "authoritative_prompt_tokens":10},
        "runtime":{"app_identity":"token.place", "runtime_identity":"bundled",
            "build_identity":"build", "backend_requested":"cpu", "backend_selected":"cpu",
            "model_fingerprint":"sha256:model", "backend_used":"cpu"},
        "progress":{"pass":True, "progress_event_count":1},
        "metrics":{"pass":True, "preparing_duration_s":0, "prefill_duration_s":1,
            "time_to_first_token_s":1, "decode_duration_s":1, "total_duration_s":2,
            "prompt_tokens":10, "output_tokens":1, "prompt_tokens_per_s":10,
            "decode_tokens_per_s":1, "request_budget_s":600, "completion_margin_s":598},
        "semantic":{"semantic_pass":semantic_pass, "exact_match":semantic_pass,
            "errors":[] if semantic_pass else ["exact_match", "target_selection"]},
        "generation_settings":{"supplied":{"max_tokens":max_tokens},
            "omitted_runtime_default":["seed", "temperature", "top_p"]},
        "memory": _memory_evidence(),
        "runtime_configuration": _runtime_configuration(),
        "kv_compare":{"pass":True, "applicability":"not_applicable_verified_non_qwen",
            "reason":"not_applicable_verified_non_qwen",
            "attestation":{"method":"active_runtime_selected_profile",
                "applicability":"not_applicable_verified_non_qwen", "architecture":"llama",
                "profile_id":"default", "backend":"cpu", "context_tier":"64k-full",
                "context_size_tokens":65536}}}


def _packaged_main_args(tmp_path, *extra):
    return ["packaged-runtime", "--out-dir", str(tmp_path), "--app-binary", "app",
        "--model", "model", "--backend", "cpu", "--relay-url", "https://relay.example", *extra]


def test_production_shaped_qwen_report_validates_and_writes_atomically(tmp_path, monkeypatch):
    evidence = _packaged_main_evidence()
    evidence["runtime"].update(backend_requested="metal", backend_selected="metal",
        backend_used="metal")
    evidence["runtime_configuration"] = _packaged_configuration_builder()(
        _packaged_runtime_labels(), _qwen_readiness_diagnostics(), "metal")
    evidence["kv_compare"] = _qwen_kv_summary()
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: evidence)
    args = ["packaged-runtime", "--out-dir", str(tmp_path), "--app-binary", "app",
        "--model", "renamed-model.gguf", "--backend", "metal",
        "--relay-url", "https://relay.example"]
    assert h.main(args) == 0
    report_path = tmp_path / "long_context_benchmark_report.json"
    report = json.loads(report_path.read_text())
    assert report["runtime_configuration"]["trials"][0]["mode"] == {
        "requested":"gpu", "effective":"metal"}
    assert report["kv_diagnostics"]["trials"][0]["type_k"] == "q8"
    h.validate_report(report)
    rewritten = h.write_report_atomic(tmp_path / "rewritten", report)
    assert json.loads(rewritten.read_text()) == report

    mutations = (
        lambda item: item["runtime_configuration"]["trials"][0]["mode"].update(effective="gpu"),
        lambda item: item["runtime_configuration"]["trials"][0]["backend"].update(
            available="cpu"),
        lambda item: item["runtime_configuration"]["trials"][0]["backend"].update(
            fallback_reason="automatic_cpu_fallback"),
        lambda item: item["runtime_configuration"]["trials"][0]["runtime_profile"].update(
            fallback_reason="null"),
        lambda item: item["runtime_configuration"]["trials"][0]["runtime_profile"].update(
            selected="qwen64k_kv_q4_fa"),
        lambda item: item["runtime_configuration"]["trials"][0]["kv_cache"].update(precision="q4"),
        lambda item: item["runtime_configuration"]["trials"][0]["kv_cache"].update(type_v=2),
        lambda item: item["backend"].update(used="cuda"),
        lambda item: item["context"].update(window_tokens=65535),
        lambda item: item["runtime_configuration"]["trials"][0]["yarn_rope"].update(
            configuration_valid=False),
    )
    for mutate in mutations:
        malformed = json.loads(json.dumps(report)); mutate(malformed)
        with pytest.raises(ValueError):
            h.validate_report(malformed)


@pytest.mark.parametrize("result", ["constructed", "failed"])
def test_completed_qwen_runtime_rejects_nonfinal_profile_results(result):
    configuration = _packaged_configuration_builder()(
        _packaged_runtime_labels(), _qwen_readiness_diagnostics(result=result), "metal")
    with pytest.raises(ValueError, match="runtime_configuration_invalid"):
        h.validate_runtime_configuration(configuration, backend="metal",
            context_tier="64k-full", context_tokens=65536,
            kv_attestation=_qwen_kv_summary())


@pytest.mark.parametrize(("architecture", "tier", "window", "reason"), [
    ("llama", "64k-full", 65536, "not_applicable_verified_non_qwen"),
    ("qwen3", "8k-fast", 8192, "not_applicable_context_tier"),
])
def test_rendered_null_profile_diagnostics_validate_end_to_end(tmp_path, monkeypatch,
        architecture, tier, window, reason):
    diagnostics = {key: "null" for key in _qwen_readiness_diagnostics()}
    configuration = _packaged_configuration_builder()(
        _packaged_runtime_labels("cpu", tier=tier, window=window), diagnostics, "cpu")
    not_applicable = {"status":"not_applicable", "reason":"not_qwen_64k_profile"}
    assert all(configuration[key] == not_applicable for key in (
        "runtime_profile", "batch_profile", "kv_cache", "acceleration", "yarn_rope"))
    evidence = _packaged_main_evidence()
    evidence["runtime_configuration"] = configuration
    evidence["kv_compare"] = {"pass":True, "applicability":reason, "reason":reason,
        "attestation":{"method":"active_runtime_selected_profile", "applicability":reason,
            "architecture":architecture, "profile_id":"default", "backend":"cpu",
            "context_tier":tier, "context_size_tokens":window}}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: evidence)
    args = _packaged_main_args(tmp_path)
    if tier == "8k-fast":
        args.extend(["--context-tier", tier])
    assert h.main(args) == 0
    report_path = tmp_path / "long_context_benchmark_report.json"
    report = json.loads(report_path.read_text())
    h.validate_report(report)
    rewritten = h.write_report_atomic(tmp_path / "rewritten", report)
    assert json.loads(rewritten.read_text()) == report


def test_packaged_trials_default_and_multiple_are_sequential(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter",
        lambda **kwargs: calls.append(len(calls)) or _packaged_main_evidence())
    assert h.main(_packaged_main_args(tmp_path)) == 0
    assert calls == [0]
    assert h.main(_packaged_main_args(tmp_path, "--trials", "3")) == 0
    assert calls == [0, 1, 2, 3]
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["requested_trial_count"] == report["completed_trial_count"] == 3
    assert report["aggregate_semantic"]["trial_count"] == 3


def test_not_run_timeout_report_retains_validated_fixture_sha_and_safe_diagnostics(tmp_path, monkeypatch):
    _, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    timeout = {"pass": False, "runtime_contract_pass": False,
        "code": "packaged_runner_timeout", "last_safe_phase": "request_active",
        "request_timeout_s": 600.0, "setup_timeout_s": h.PACKAGED_SETUP_BUDGET_S,
        "finalization_timeout_s": h.PACKAGED_FINALIZATION_BUDGET_S,
        "cancellation_timeout_s": 0.0,
        "cleanup_timeout_s": 30.0,
        "runner_timeout_s": h.PACKAGED_SETUP_BUDGET_S + 600 + h.PACKAGED_FINALIZATION_BUDGET_S,
        "overall_timeout_s": h.PACKAGED_SETUP_BUDGET_S + 600
            + h.PACKAGED_FINALIZATION_BUDGET_S + 30,
        "elapsed_s": 700.0, "cleanup_succeeded": True}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: timeout)
    assert h.main(_packaged_main_args(tmp_path, "--fixture", "small-8k", "--scenario",
        "single-needle", "--request-timeout", "600", "--cleanup-timeout", "30",
        "--report-only")) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["fixture"]["sha256"] == manifest["fixture_sha256"]
    assert report["completed_trial_count"] == 0
    assert report["last_safe_phase"] == "request_active"
    assert not h.SENSITIVE_KEYS.intersection(report)


def test_not_run_runner_failure_retains_only_allowlisted_safe_diagnostics(tmp_path, monkeypatch):
    _, manifest = h.generate_fixture("small-8k", scenario="single-needle")
    failure = {"pass": False, "runtime_contract_pass": False,
        "code": "packaged_runner_failed", "last_safe_phase": "operator_ready",
        "failure_reason": "client_keypair_not_ready", "elapsed_s": 299.0,
        "cleanup_succeeded": True, "prompt": "secret fixture prompt",
        "traceback": "Traceback: secret", "diagnostic_tail": "private log text",
        "path": "/private/model.gguf", "request_id": "request-secret",
        "client_id": "client-secret", "session_id": "session-secret"}
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: failure)
    assert h.main(_packaged_main_args(tmp_path, "--fixture", "small-8k", "--scenario",
        "single-needle", "--report-only")) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    h.validate_report(report)
    assert report["fixture"]["sha256"] == manifest["fixture_sha256"]
    assert report["completed_trial_count"] == 0
    assert report["failure_reason"] in h.PACKAGED_FAILURE_REASONS
    prohibited = {"prompt", "response_text", "diagnostic_tail", "traceback", "path",
        "request_id", "client_id", "session_id", "ciphertext", "key"}
    assert not prohibited.intersection(report)
    assert report["last_safe_phase"] == "operator_ready"
    assert report["elapsed_s"] == 299.0
    assert report["cleanup_succeeded"] is True


@pytest.mark.parametrize("timeout_field", [
    "request_timeout_s", "setup_timeout_s", "runner_timeout_s", "overall_timeout_s",
])
def test_runner_failure_report_rejects_timeout_budget_fields(timeout_field):
    report = {"schema_version": h.SCHEMA_VERSION, "mode": "packaged-runtime",
        "status": "not_run", "fixture": {"id": "small-8k", "version": h.FIXTURE_VERSION,
            "scenario": "single-needle", "sha256": "unavailable"},
        "code": "packaged_runner_failed", "last_safe_phase": "operator_ready",
        "failure_reason": "client_keypair_not_ready", "elapsed_s": 1.0,
        "cleanup_succeeded": True, timeout_field: 600.0}
    with pytest.raises(ValueError, match="report_runner_failure_diagnostics_invalid"):
        h.validate_report(report)


@pytest.mark.parametrize(("field", "value"), [
    ("failure_reason", []), ("failure_reason", {}),
    ("cleanup_succeeded", []), ("cleanup_succeeded", {}),
    ("elapsed_s", []), ("last_safe_phase", {}),
])
def test_runner_failure_report_rejects_malformed_diagnostic_types(field, value):
    report = {"schema_version": h.SCHEMA_VERSION, "mode": "packaged-runtime",
        "status": "not_run", "fixture": {"id": "small-8k", "version": h.FIXTURE_VERSION,
            "scenario": "single-needle", "sha256": "unavailable"},
        "code": "packaged_runner_failed", "last_safe_phase": "operator_ready",
        "failure_reason": "client_keypair_not_ready", "elapsed_s": 1.0,
        "cleanup_succeeded": True}
    report[field] = value
    with pytest.raises(ValueError, match="report_runner_failure_diagnostics_invalid"):
        h.validate_report(report)


def test_not_run_invalid_external_manifest_uses_safe_fixture_sha(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.txt"
    manifest = tmp_path / "manifest.json"
    prompt.write_text("external prompt", encoding="utf-8")
    manifest.write_text(json.dumps({"fixture_id": "small-8k"}), encoding="utf-8")
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: {
        "pass": False, "runtime_contract_pass": False,
        "code": "manifest_missing_fields"})

    assert h.main(_packaged_main_args(tmp_path, "--prompt", str(prompt),
        "--manifest", str(manifest))) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["status"] == "not_run"
    assert report["code"] == "manifest_missing_fields"
    assert report["fixture"]["sha256"] == "unavailable"


@pytest.mark.parametrize("manifest_value", ["not-json", "[]"])
def test_not_run_malformed_external_manifest_is_categorical(tmp_path, manifest_value, monkeypatch):
    prompt = tmp_path / "prompt.txt"; prompt.write_text("external prompt", encoding="utf-8")
    manifest = tmp_path / "manifest.json"; manifest.write_text(manifest_value, encoding="utf-8")
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **_kwargs: {
        "pass": False, "runtime_contract_pass": False, "code": "manifest_not_object"})
    assert h.main(_packaged_main_args(tmp_path, "--prompt", str(prompt),
        "--manifest", str(manifest), "--report-only")) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["status"] == "not_run"
    assert report["code"] == "manifest_not_object"
    assert report["fixture"]["sha256"] == "unavailable"


def test_cancellation_sequence_runs_once_outside_semantic_trial_count(tmp_path, monkeypatch):
    calls = []
    def fake_invoke(**kwargs):
        calls.append(kwargs["cancellation_validation"])
        result = _packaged_main_evidence()
        if kwargs["cancellation_validation"]:
            result["cancellation_recovery"] = {
                **_physical_cancellation_evidence(), "pass": True}
        return result
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", fake_invoke)
    args = _packaged_main_args(tmp_path, "--trials", "3", "--cancellation-validation",
        "--prefill-cancel-tokens", "50", "--generation-cancel-tokens", "8")
    assert h.main(args) == 0
    assert calls == [True, False, False]
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["aggregate_semantic"]["trial_count"] == 3
    assert report["cancellation_recovery"]["pass"] is True


@pytest.mark.parametrize("extra", [
    ("--cancellation-validation",),
    ("--cancellation-validation", "--prefill-cancel-tokens", "0"),
    ("--cancellation-validation", "--prefill-cancel-fraction", "1"),
    ("--cancellation-validation", "--prefill-cancel-tokens", "1",
        "--generation-cancel-tokens", "0"),
])
def test_cancellation_cli_configuration_is_bounded(tmp_path, extra):
    with pytest.raises(SystemExit, match="2"):
        h.main(_packaged_main_args(tmp_path, *extra))


def test_packaged_trials_aggregate_mixed_semantics_and_report_only(tmp_path, monkeypatch):
    outcomes = iter([True, False, True])
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter",
        lambda **kwargs: _packaged_main_evidence(next(outcomes)))
    assert h.main(_packaged_main_args(tmp_path, "--trials", "3", "--report-only")) == 0
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    aggregate = report["aggregate_semantic"]
    assert report["overall_pass"] is False and report["report_only_accepted"] is True
    assert aggregate["exact_match_count"] == 2 and aggregate["pass_rate"] == pytest.approx(2 / 3)
    assert aggregate["failure_categories"] == {"exact_match": 1, "target_selection": 1}
    serialized = json.dumps(report).lower()
    assert all(word not in serialized for word in ("response_text", "messages", "ciphertext", "request_id"))


def test_packaged_trials_fail_closed_on_runtime_failure_and_settings_drift(tmp_path, monkeypatch):
    runtime_failure = {"pass":False, "runtime_contract_pass":False, "code":"telemetry_failed"}
    outcomes = iter([_packaged_main_evidence(), runtime_failure, _packaged_main_evidence()])
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **kwargs: next(outcomes))
    assert h.main(_packaged_main_args(tmp_path, "--trials", "3", "--report-only")) == 1
    report = json.loads((tmp_path / "long_context_benchmark_report.json").read_text())
    assert report["requested_trial_count"] == 3 and report["completed_trial_count"] == 1
    assert report["code"] == "telemetry_failed"

    outcomes = iter([_packaged_main_evidence(), _packaged_main_evidence(max_tokens=512)])
    monkeypatch.setattr(h, "invoke_packaged_runtime_adapter", lambda **kwargs: next(outcomes))
    assert h.main(_packaged_main_args(tmp_path, "--trials", "2", "--report-only")) == 1
    assert json.loads((tmp_path / "long_context_benchmark_report.json").read_text())["code"] == "generation_settings_inconsistent"


@pytest.mark.parametrize("trials", ["0", "-1", str(h.MAX_PACKAGED_TRIALS + 1)])
def test_packaged_trials_argument_is_bounded(tmp_path, trials):
    with pytest.raises(SystemExit, match="2"):
        h.main(_packaged_main_args(tmp_path, "--trials", trials))


@pytest.mark.parametrize(("settings", "code"), [
    (None, "generation_settings_malformed"),
    ({"supplied":{}, "omitted_runtime_default":[]}, "generation_settings_malformed"),
    ({"supplied":{"temperature":1}, "omitted_runtime_default":["max_tokens", "seed", "top_p"]},
     "generation_settings_unsupported"),
    ({"supplied":{"max_tokens":float("nan")}, "omitted_runtime_default":["seed", "temperature", "top_p"]},
     "generation_settings_value_invalid"),
    ({"supplied":{"max_tokens":1024, "unknown":1}, "omitted_runtime_default":["seed", "temperature", "top_p"]},
     "generation_settings_unsupported"),
    ({"supplied":{"max_tokens":1024}, "omitted_runtime_default":[]},
     "generation_settings_omissions_invalid"),
])
def test_generation_settings_validation_fails_closed(settings, code):
    with pytest.raises(ValueError, match=code):
        h.validate_generation_settings(settings)
