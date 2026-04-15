#!/usr/bin/env python3
"""NDJSON inference sidecar that reuses the shared Python model runtime."""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Tuple

if __package__ in (None, ""):
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

from path_bootstrap import ensure_runtime_import_paths
from desktop_runtime_setup import ensure_desktop_llama_runtime, RUNTIME_REEXEC_ENV

ensure_runtime_import_paths(__file__)

_stdin_lines: queue.Queue[str] = queue.Queue()
_stdin_reader_started = False
_stdin_reader_lock = threading.Lock()


def _start_stdin_reader() -> None:
    global _stdin_reader_started
    with _stdin_reader_lock:
        if _stdin_reader_started:
            return

        def _reader() -> None:
            while True:
                line = sys.stdin.readline()
                if line == "":
                    break
                _stdin_lines.put(line)

        threading.Thread(target=_reader, daemon=True).start()
        _stdin_reader_started = True


def emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def emit_error(code: str, message: str) -> int:
    emit({"type": "error", "code": code, "message": message})
    return 1


def emit_summary(label: str, **fields: Any) -> None:
    summary_payload = {"label": label}
    summary_payload.update({key: value for key, value in fields.items() if value is not None})
    print(
        f"desktop.inference.summary {json.dumps(summary_payload, separators=(',', ':'))}",
        file=sys.stderr,
        flush=True,
    )


def verbose_subprocess_logging_enabled() -> bool:
    return os.getenv("TOKEN_PLACE_VERBOSE_SUBPROCESS_LOGS") == "1" or os.getenv(
        "TOKEN_PLACE_VERBOSE_LLM_LOGS"
    ) == "1"


def cancel_requested() -> bool:
    _start_stdin_reader()
    while True:
        try:
            line = _stdin_lines.get_nowait().strip()
        except queue.Empty:
            return False
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "cancel":
            return True


def _fallback_normalize_chunk(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk

    for attr in ("to_dict", "model_dump", "dict"):
        handler = getattr(chunk, attr, None)
        if callable(handler):
            try:
                parsed = handler()
            except TypeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    if hasattr(chunk, "__dict__") and isinstance(chunk.__dict__, dict):
        return chunk.__dict__
    return {}


def _stream_content(
    completion: Iterable[Any],
    normalize_chunk: Callable[[Any], Dict[str, Any]],
    token_counter: list[int] | None = None,
) -> Tuple[str, bool]:
    full_text = []
    emitted = False
    token_events = 0
    for raw_chunk in completion:
        if cancel_requested():
            emit({"type": "canceled"})
            if token_counter is not None:
                token_counter.append(token_events)
            return "", True

        chunk = normalize_chunk(raw_chunk)
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = (choices[0] or {}).get("delta") or {}
        if not isinstance(delta, dict):
            continue

        text = delta.get("content")
        if text:
            full_text.append(text)
            emitted = True
            emit({"type": "token", "text": text})
            token_events += 1

        if (choices[0] or {}).get("finish_reason"):
            break

    if token_counter is not None:
        token_counter.append(token_events)
    return "".join(full_text) if emitted else "", False


def _extract_text_from_completion(completion: Dict[str, Any]) -> str:
    choices = completion.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    return message.get("content", "") if isinstance(message, dict) else ""


def run(args: argparse.Namespace) -> int:
    if not os.path.exists(args.model):
        return emit_error("bad_model", "model path not found")

    try:
        from utils.compute_node_runtime import apply_compute_mode, compute_mode_diagnostics
        from utils.llm.model_manager import ModelManager, get_model_manager
    except ModuleNotFoundError as exc:
        return emit_error(
            "runtime_unavailable",
            f"Missing Python dependency for local inference ({exc}).",
        )

    runtime_setup = ensure_desktop_llama_runtime(args.mode)
    print(
        "desktop.runtime_setup "
        f"mode={args.mode} "
        f"selected_backend={runtime_setup.get('selected_backend', 'cpu')} "
        f"device={runtime_setup.get('detected_device', 'cpu')} "
        f"action={runtime_setup.get('runtime_action', 'none')} "
        f"python={runtime_setup.get('python_executable', sys.executable)} "
        f"llama_cpp={runtime_setup.get('llama_cpp_path', 'unknown')} "
        f"fallback_reason={runtime_setup.get('fallback_reason') or 'none'}",
        file=sys.stderr,
    )
    if (
        runtime_setup.get("runtime_action", "").endswith("_reexec_required")
        and os.environ.get(RUNTIME_REEXEC_ENV) != "1"
    ):
        os.environ[RUNTIME_REEXEC_ENV] = "1"
        print(
            "desktop.runtime_setup_reexec reason=runtime_install "
            f"python={sys.executable} action={runtime_setup.get('runtime_action')}",
            file=sys.stderr,
        )
        os.execv(sys.executable, [sys.executable, *sys.argv])

    manager = get_model_manager()
    manager.model_path = args.model
    apply_compute_mode(manager, args.mode)

    model_load_started_at = time.perf_counter()
    llm = manager.get_llm_instance()
    model_load_elapsed_ms = int((time.perf_counter() - model_load_started_at) * 1000)
    if llm is None:
        return emit_error("bad_model", "unable to initialize model runtime")

    diagnostics = compute_mode_diagnostics(manager)
    model_name = Path(args.model).name
    model_path_for_summary = args.model if verbose_subprocess_logging_enabled() else model_name
    emit_summary(
        "model_init",
        model=model_name,
        model_path=model_path_for_summary,
        backend=diagnostics.get("backend_used"),
        device=diagnostics.get("effective_mode"),
        context_size=manager.config.get("model.context_size", 8192),
        offloaded_layers=diagnostics.get("n_gpu_layers"),
        load_time_ms=model_load_elapsed_ms,
        fallback_reason=diagnostics.get("fallback_reason"),
    )
    emit(
        {
            "type": "started",
            "requested_mode": diagnostics.get("requested_mode"),
            "effective_mode": diagnostics.get("effective_mode"),
            "backend_used": diagnostics.get("backend_used"),
            "n_gpu_layers": diagnostics.get("n_gpu_layers"),
            "fallback_reason": diagnostics.get("fallback_reason"),
        }
    )
    if cancel_requested():
        emit({"type": "canceled"})
        return 0

    messages = [{"role": "user", "content": args.prompt}]
    request_kwargs = {
        "messages": messages,
        "max_tokens": manager.config.get("model.max_tokens", 512),
        "temperature": manager.config.get("model.temperature", 0.7),
        "top_p": manager.config.get("model.top_p", 0.9),
        "stop": manager.config.get("model.stop_tokens", []),
    }
    inference_started_at = time.perf_counter()
    try:
        completion = llm.create_chat_completion(
            **request_kwargs,
            stream=True,
        )
    except Exception as exc:  # pragma: no cover - defensive runtime handling
        return emit_error("inference_failed", f"streaming completion failed: {exc}")

    normalize_chunk = getattr(ModelManager, "_normalize_stream_chunk", _fallback_normalize_chunk)

    token_events = 0
    text = ""
    if isinstance(completion, dict):
        text = _extract_text_from_completion(completion)
        if text:
            emit({"type": "token", "text": text})
            token_events += 1
    else:
        token_counts: list[int] = []
        text, canceled = _stream_content(completion, normalize_chunk, token_counts)
        token_events = token_counts[0] if token_counts else 0
        if canceled:
            return 0

        if not text:
            try:
                fallback = llm.create_chat_completion(
                    **request_kwargs,
                    stream=False,
                )
            except Exception as exc:  # pragma: no cover - defensive runtime handling
                return emit_error("inference_failed", f"non-streaming completion failed: {exc}")

            fallback_text = _extract_text_from_completion(fallback)
            if fallback_text:
                emit({"type": "token", "text": fallback_text})
                token_events = max(token_events, 1)
                text = fallback_text

    inference_elapsed_s = max(time.perf_counter() - inference_started_at, 1e-6)
    prompt_chars_per_second = int(len(args.prompt) / inference_elapsed_s) if args.prompt else 0
    eval_chars_per_second = int(len(text) / inference_elapsed_s) if text else 0
    eval_tokens_per_second = round(token_events / inference_elapsed_s, 2) if token_events else 0
    emit_summary(
        "inference",
        prompt_chars=len(args.prompt),
        output_chars=len(text),
        token_events=token_events,
        eval_seconds=f"{inference_elapsed_s:.3f}",
        throughput_summary=(
            f"prompt_chars_per_s={prompt_chars_per_second};"
            f"eval_chars_per_s={eval_chars_per_second};"
            f"eval_tokens_per_s={eval_tokens_per_second}"
        ),
    )

    emit({"type": "done"})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop inference sidecar")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()
    try:
        from utils.compute_node_runtime import normalize_compute_mode

        args.mode = normalize_compute_mode(args.mode)
        return run(args)
    except Exception as exc:  # pragma: no cover - last resort error handling
        return emit_error("inference_failed", f"bridge failure: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
