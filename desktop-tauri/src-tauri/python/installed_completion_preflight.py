#!/usr/bin/env python3
"""Bounded, privacy-safe installed GPU completion qualification.

This module is deliberately a local-only command.  It never starts relay polling;
the production ``ComputeNodeRuntime.ensure_api_v1_runtime_ready`` boundary performs
the single synthetic, non-thinking API-v1 completion through the child worker.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from path_bootstrap import ensure_runtime_import_paths

ensure_runtime_import_paths(__file__, avoid_llama_cpp_shadowing=True)

SCHEMA = "token.place/installed-gpu-completion-preflight/v1"
OUTPUT_MAX_TOKENS = 64
STARTUP_DEADLINE_SECONDS = 15.0
MODEL_LOAD_DEADLINE_SECONDS = 120.0
GENERATION_DEADLINE_SECONDS = 45.0
CANCELLATION_DEADLINE_SECONDS = 5.0
CLEANUP_DEADLINE_SECONDS = 10.0
TOTAL_DEADLINE_SECONDS = 180.0
ALLOWED_BACKENDS = {"cuda", "metal"}


def _evidence(identity: Dict[str, str]) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "accepted": False,
        "failure_code": "not_started",
        "identity": identity,
        "configuration": {"output_max_tokens": OUTPUT_MAX_TOKENS},
        "deadlines_ms": {
            "startup": int(STARTUP_DEADLINE_SECONDS * 1000),
            "model_load": int(MODEL_LOAD_DEADLINE_SECONDS * 1000),
            "generation": int(GENERATION_DEADLINE_SECONDS * 1000),
            "cancellation": int(CANCELLATION_DEADLINE_SECONDS * 1000),
            "cleanup": int(CLEANUP_DEADLINE_SECONDS * 1000),
            "total": int(TOTAL_DEADLINE_SECONDS * 1000),
        },
        "phases": {name: "not_started" for name in ("identity", "startup", "model_load_generation", "cleanup")},
        "timings_ms": {},
        "observed_execution": {"backend_used": "unknown", "offloaded_layers": 0, "kv_cache_device": "unknown"},
        "completion_count": 0,
        "cleanup": {"requested": False, "verified": False},
        "side_effects": {"relay_contacts": 0, "registrations": 0, "benchmark_attempts": 0},
    }


def _identity_from_env() -> Dict[str, str]:
    return {key: os.environ.get(env, "") for key, env in (
        ("app_version", "TOKENPLACE_APP_VERSION"),
        ("build_id", "TOKENPLACE_BUILD_ID"),
        ("target_triple", "TOKENPLACE_TARGET_TRIPLE"),
        ("bundled_runtime_id", "TOKENPLACE_BUNDLED_RUNTIME_ID"),
        ("runtime_id", "TOKENPLACE_RUNTIME_ID"),
        ("launcher_source", "TOKENPLACE_LAUNCHER_SOURCE"),
    )}


def run_preflight(
    args: argparse.Namespace,
    *,
    runtime_factory: Optional[Callable[[], Any]] = None,
    setup_runtime: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> tuple[int, Dict[str, Any]]:
    """Run the qualification; dependency seams are only for deterministic tests."""
    started = time.monotonic()
    identity = _identity_from_env()
    result = _evidence(identity)
    runtime = None
    exit_code = 1
    try:
        if (
            not all(identity.values())
            or identity["launcher_source"] != "bundled"
            or identity["runtime_id"] != identity["bundled_runtime_id"]
        ):
            result["failure_code"] = "packaged_identity_mismatch"
            return 2, result
        result["phases"]["identity"] = "passed"
        model = Path(args.model)
        from utils.llm.model_profiles import get_default_model_profile
        expected_model = str(get_default_model_profile()["filename"])
        result["identity"]["model_filename"] = model.name
        result["identity"]["approved_model_filename"] = expected_model
        if not model.is_file() or model.name != expected_model:
            result["failure_code"] = "model_identity_mismatch"
            return 3, result

        if setup_runtime is None:
            from desktop_runtime_setup import ensure_desktop_llama_runtime
            setup_runtime = lambda mode: ensure_desktop_llama_runtime(mode, context_tier=args.context_tier)
        setup = setup_runtime(args.mode)
        selected = setup.get("selected_backend")
        result["identity"]["declared_backend"] = str(selected or "unknown")
        if selected not in ALLOWED_BACKENDS:
            result["failure_code"] = "cpu_fallback_rejected"
            return 4, result
        result["phases"]["startup"] = "passed"

        if runtime_factory is None:
            from utils.compute_node_runtime import ComputeNodeRuntime, ComputeNodeRuntimeConfig, apply_compute_mode
            runtime = ComputeNodeRuntime(ComputeNodeRuntimeConfig(
                relay_url="http://127.0.0.1:1", relay_port=1,
                use_configured_relay_fallbacks=False, relay_urls=("http://127.0.0.1:1",),
            ))
            manager = runtime.model_manager
            manager.model_path = str(model.resolve())
            manager.parent_model_path_exists = True
            manager.model_path_was_relative = False
            apply_compute_mode(manager, args.mode)
            manager.desktop_runtime_probe = dict(setup)
        else:
            runtime = runtime_factory()
            manager = runtime.model_manager

        os.environ["TOKEN_PLACE_API_V1_READINESS_SMOKE_COMPLETION"] = "1"
        phase_started = time.monotonic()
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(runtime.ensure_api_v1_runtime_ready)
            try:
                ready = future.result(timeout=MODEL_LOAD_DEADLINE_SECONDS + GENERATION_DEADLINE_SECONDS)
            except concurrent.futures.TimeoutError:
                result["failure_code"] = "completion_deadline_exceeded"
                future.cancel()
                return 5, result
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        result["timings_ms"]["model_load_generation"] = int((time.monotonic() - phase_started) * 1000)
        diagnostics = getattr(manager, "last_compute_diagnostics", {}) or {}
        backend = diagnostics.get("backend_used")
        offloaded = diagnostics.get("offloaded_layers", diagnostics.get("n_gpu_layers", 0))
        kv_device = diagnostics.get("kv_cache_device", "unknown")
        result["observed_execution"] = {
            "backend_used": backend if backend in ALLOWED_BACKENDS else "unknown",
            "offloaded_layers": offloaded if isinstance(offloaded, int) and not isinstance(offloaded, bool) else 0,
            "kv_cache_device": kv_device if kv_device in {"cuda", "metal", "gpu"} else "unknown",
        }
        smoke_result = diagnostics.get("api_v1_readiness_completion_smoke_result")
        attempts = diagnostics.get("api_v1_readiness_completion_smoke_plain_completion_attempt_count", 1 if smoke_result == "passed" else 0)
        result["completion_count"] = attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0
        if not ready or smoke_result != "passed":
            result["failure_code"] = "completion_failed"
            return 6, result
        if result["completion_count"] != 1:
            result["failure_code"] = "completion_count_invalid"
            return 7, result
        if backend not in ALLOWED_BACKENDS or result["observed_execution"]["offloaded_layers"] <= 0:
            result["failure_code"] = "gpu_execution_unverified"
            return 8, result
        result["phases"]["model_load_generation"] = "passed"
        result["failure_code"] = "none"
        exit_code = 0
    except Exception:
        result["failure_code"] = "internal_failure"
        exit_code = 9
    finally:
        result["cleanup"]["requested"] = True
        cleanup_started = time.monotonic()
        cleanup_ok = runtime is None
        if runtime is not None:
            try:
                runtime.stop(shutdown_deadline=time.monotonic() + CLEANUP_DEADLINE_SECONDS)
                manager = runtime.model_manager
                loaded = getattr(manager, "llm", None)
                if loaded is not None:
                    closer = getattr(manager, "_close_llm_proxy", None)
                    cleanup_ok = callable(closer) and closer(loaded) is True
                    if cleanup_ok:
                        manager.llm = None
                else:
                    cleanup_ok = True
            except Exception:
                cleanup_ok = False
        result["timings_ms"]["cleanup"] = int((time.monotonic() - cleanup_started) * 1000)
        result["cleanup"]["verified"] = cleanup_ok
        result["phases"]["cleanup"] = "passed" if cleanup_ok else "failed"
        result["timings_ms"]["total"] = int((time.monotonic() - started) * 1000)
        if not cleanup_ok:
            result["failure_code"] = "cleanup_failed"
            exit_code = 10
        elif result["failure_code"] != "none" and exit_code == 1:
            exit_code = {
                "packaged_identity_mismatch": 2, "model_identity_mismatch": 3,
                "cpu_fallback_rejected": 4, "completion_deadline_exceeded": 5,
                "completion_failed": 6, "completion_count_invalid": 7,
                "gpu_execution_unverified": 8, "internal_failure": 9,
            }.get(result["failure_code"], 1)
        result["accepted"] = exit_code == 0 and cleanup_ok
        return exit_code, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", required=True, choices=("auto", "gpu", "hybrid", "cuda", "metal"))
    parser.add_argument("--context-tier", default="8k-fast", choices=("8k-fast", "64k-full"))
    args = parser.parse_args()
    code, evidence = run_preflight(args)
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
