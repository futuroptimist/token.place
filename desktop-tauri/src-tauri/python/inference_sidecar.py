#!/usr/bin/env python3
"""NDJSON inference sidecar that reuses the shared Python model runtime."""

from __future__ import annotations

import argparse
import json
import logging
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


def _desktop_verbose_logs_enabled() -> bool:
    return os.getenv("TOKEN_PLACE_DESKTOP_VERBOSE_LOGS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _configure_desktop_logging() -> None:
    if _desktop_verbose_logs_enabled():
        return
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("model_manager").setLevel(logging.WARNING)


def _estimate_token_count(text: str) -> int:
    stripped = (text or "").strip()
    if not stripped:
        return 0
    return len(stripped.split())


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
) -> Tuple[str, bool]:
    full_text = []
    emitted = False
    for raw_chunk in completion:
        if cancel_requested():
            emit({"type": "canceled"})
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

        if (choices[0] or {}).get("finish_reason"):
            break

    return "".join(full_text) if emitted else "", False


def _extract_text_from_completion(completion: Dict[str, Any]) -> str:
    choices = completion.get("choices") or [{}]
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    return message.get("content", "") if isinstance(message, dict) else ""


def run(args: argparse.Namespace) -> int:
    _configure_desktop_logging()
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

    manager = get_model_manager()
    manager.model_path = args.model
    apply_compute_mode(manager, args.mode)

    init_start = time.perf_counter()
    llm = manager.get_llm_instance()
    init_elapsed_ms = int((time.perf_counter() - init_start) * 1000)
    if llm is None:
        return emit_error("bad_model", "unable to initialize model runtime")

    diagnostics = compute_mode_diagnostics(manager)
    model_name = Path(args.model).name
    context_size = manager.config.get("model.context_size", 8192)
    emit(
        {
            "type": "started",
            "model_path": args.model,
            "model_name": model_name,
            "requested_mode": diagnostics.get("requested_mode"),
            "effective_mode": diagnostics.get("effective_mode"),
            "backend_used": diagnostics.get("backend_used"),
            "n_gpu_layers": diagnostics.get("n_gpu_layers"),
            "context_size": context_size,
            "load_ms": init_elapsed_ms,
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
    inference_start = time.perf_counter()
    generated_text = ""
    try:
        completion = llm.create_chat_completion(
            **request_kwargs,
            stream=True,
        )
    except Exception as exc:  # pragma: no cover - defensive runtime handling
        return emit_error("inference_failed", f"streaming completion failed: {exc}")

    normalize_chunk = getattr(ModelManager, "_normalize_stream_chunk", _fallback_normalize_chunk)

    if isinstance(completion, dict):
        text = _extract_text_from_completion(completion)
        generated_text = text
        if text:
            emit({"type": "token", "text": text})
    else:
        text, canceled = _stream_content(completion, normalize_chunk)
        generated_text = text
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
            generated_text = fallback_text
            if fallback_text:
                emit({"type": "token", "text": fallback_text})

    inference_elapsed = max(time.perf_counter() - inference_start, 0.000001)
    prompt_tokens_estimate = _estimate_token_count(args.prompt)
    eval_tokens_estimate = _estimate_token_count(generated_text)
    emit(
        {
            "type": "done",
            "prompt_chars": len(args.prompt),
            "output_chars": len(generated_text),
            "prompt_tokens_estimate": prompt_tokens_estimate,
            "eval_tokens_estimate": eval_tokens_estimate,
            "prompt_tokens_per_second": round(prompt_tokens_estimate / inference_elapsed, 2),
            "eval_tokens_per_second": round(eval_tokens_estimate / inference_elapsed, 2),
            "inference_ms": int(inference_elapsed * 1000),
        }
    )
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
