"""Permanent installed-package CPU warm-load and authoritative admission boundary."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _result(warm: str, evidence: str) -> None:
    print(
        json.dumps(
            {
                "schema_version": 1,
                "warm_load": warm,
                "authoritative_evidence": evidence,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )


def run(model: Path, context_tier: str) -> int:
    # This command is release-facing: never admit source/mock substitutions.
    if os.environ.get("USE_MOCK_LLM", "").lower() in {"1", "true", "yes"}:
        _result("failed", "failed")
        return 20
    from compute_node_bridge import (
        _ensure_desktop_llama_runtime_for_context,
        _load_context_profile_helpers,
        ensure_desktop_python_dependencies,
    )

    if ensure_desktop_python_dependencies().get("ok") != "true":
        _result("failed", "failed")
        return 21
    setup = _ensure_desktop_llama_runtime_for_context("cpu", context_tier)
    if setup.get("selected_backend") != "cpu":
        _result("failed", "failed")
        return 22
    from utils.compute_node_runtime import (
        ComputeNodeRuntime,
        ComputeNodeRuntimeConfig,
        apply_compute_mode,
    )
    from utils.networking.relay_client import RelayClient

    apply_context_profile, normalize_tier = _load_context_profile_helpers()
    runtime = ComputeNodeRuntime(
        ComputeNodeRuntimeConfig(
            relay_url="http://127.0.0.1:1",
            relay_port=1,
            use_configured_relay_fallbacks=False,
            relay_urls=("http://127.0.0.1:1",),
        )
    )
    manager = runtime.model_manager
    manager.model_path = str(model)
    apply_context_profile(manager, normalize_tier(context_tier))
    apply_compute_mode(manager, "cpu")
    llm: Any = None
    try:
        llm = manager.get_llm_instance()
        if llm is None:
            _result("failed", "failed")
            return 23
        fixture = "token.place installed admission fixture alpha beta gamma"
        messages = [{"role": "user", "content": fixture}]
        profile = (
            manager.model_profile if isinstance(manager.model_profile, dict) else {}
        )
        total = RelayClient._api_v1_render_and_tokenize_chat_prompt(
            llm, messages, enable_thinking=False, model_profile=profile
        )
        if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
            _result("ready", "failed")
            return 24
        raw = fixture.encode("utf-8")
        offset = len("token.place installed admission fixture".encode("utf-8"))
        with tempfile.TemporaryDirectory(prefix="tokenplace-admission-") as directory:
            request = Path(directory) / "request.json"
            evidence = Path(directory) / "evidence.json"
            request.write_text(
                json.dumps(
                    {
                        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
                        "target_prefix_utf8_bytes": {"boundary": offset},
                    }
                ),
                encoding="utf-8",
            )
            os.environ["TOKEN_PLACE_LONG_CONTEXT_BENCHMARK_TOKENIZER_REQUEST"] = str(
                request
            )
            os.environ["TOKEN_PLACE_LONG_CONTEXT_BENCHMARK_TOKENIZER_EVIDENCE"] = str(
                evidence
            )
            RelayClient._api_v1_record_benchmark_tokenizer_observation(
                llm,
                messages,
                full_prompt_tokens=total,
                enable_thinking=False,
                model_profile=profile,
            )
            try:
                observed = json.loads(evidence.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                observed = {}
            valid = (
                observed.get("method") == "packaged_admission_render_and_tokenize_chat"
                and observed.get("runtime_identity")
                == os.environ.get("TOKENPLACE_RUNTIME_ID")
                and observed.get("fixture_sha256") == hashlib.sha256(raw).hexdigest()
                and observed.get("total_prompt_tokens") == total
                and isinstance(
                    observed.get("target_offsets_tokens", {}).get("boundary"), int
                )
                and observed["target_offsets_tokens"]["boundary"] > 0
            )
        _result("ready", "passed" if valid else "failed")
        return 0 if valid else 25
    except Exception:
        _result("failed" if llm is None else "ready", "failed")
        return 26
    finally:
        close = getattr(manager, "close", None) or getattr(manager, "shutdown", None)
        if callable(close):
            close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--context-tier", required=True)
    args = parser.parse_args()
    return run(args.model, args.context_tier)


if __name__ == "__main__":
    raise SystemExit(main())
