#!/usr/bin/env python3
"""NDJSON inference bridge for the desktop app backed by shared Python runtime."""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.llm.model_manager import ModelManager


_STDIN_LINES: queue.Queue[str] = queue.Queue()
_STDIN_STARTED = False
_STDIN_LOCK = threading.Lock()


class BridgeConfig:
    """Minimal config adapter for ModelManager without full server imports."""

    def __init__(self, *, model_path: str):
        model_file = os.path.basename(model_path)
        models_dir = str(Path(model_path).resolve().parent)
        self.is_production = False
        self._values: Dict[str, Any] = {
            "model.filename": model_file,
            "paths.models_dir": models_dir,
            "model.max_tokens": int(os.environ.get("TOKEN_PLACE_MAX_TOKENS", "512")),
            "model.temperature": float(os.environ.get("TOKEN_PLACE_TEMPERATURE", "0.7")),
            "model.top_p": float(os.environ.get("TOKEN_PLACE_TOP_P", "0.9")),
            "model.stop_tokens": [],
            "model.use_mock": os.environ.get("USE_MOCK_LLM") == "1",
            "model.n_gpu_layers": int(os.environ.get("TOKEN_PLACE_N_GPU_LAYERS", "-1")),
            "model.gpu_memory_headroom_percent": float(
                os.environ.get("TOKEN_PLACE_GPU_HEADROOM_PERCENT", "0.1")
            ),
            "model.enforce_gpu_memory_headroom": True,
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._values[key] = value


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _start_stdin_reader() -> None:
    global _STDIN_STARTED
    with _STDIN_LOCK:
        if _STDIN_STARTED:
            return

        def _reader() -> None:
            while True:
                line = sys.stdin.readline()
                if line == "":
                    break
                _STDIN_LINES.put(line)

        threading.Thread(target=_reader, daemon=True).start()
        _STDIN_STARTED = True


def _cancel_requested() -> bool:
    _start_stdin_reader()
    while True:
        try:
            line = _STDIN_LINES.get_nowait().strip()
        except queue.Empty:
            return False

        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        if payload.get("type") == "cancel":
            return True


def _normalise_chunk(chunk: Any) -> Dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk

    for attr in ("to_dict", "model_dump", "dict"):
        handler = getattr(chunk, attr, None)
        if callable(handler):
            try:
                normalised = handler()
            except TypeError:
                continue
            if isinstance(normalised, dict):
                return normalised

    if hasattr(chunk, "__dict__") and isinstance(chunk.__dict__, dict):
        return chunk.__dict__

    return {}


def _stream_completion_tokens(completion: Any) -> Iterable[str]:
    if isinstance(completion, dict):
        message = ((completion.get("choices") or [{}])[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content:
            yield content
        return

    for raw_chunk in completion:
        chunk = _normalise_chunk(raw_chunk)
        choices = chunk.get("choices") or []
        if not choices:
            continue

        delta = (choices[0] or {}).get("delta") or {}
        if not isinstance(delta, dict):
            continue

        content = delta.get("content")
        if isinstance(content, str) and content:
            yield content


def run_inference(*, model_path: str, mode: str, prompt: str) -> int:
    _ = mode
    if not os.path.exists(model_path):
        _emit({"type": "error", "code": "bad_model", "message": "model path not found"})
        return 2

    runtime_config = BridgeConfig(model_path=model_path)
    manager = ModelManager(config=runtime_config)
    manager.model_path = model_path
    manager.llm = None

    llm = manager.get_llm_instance()
    if llm is None:
        _emit(
            {
                "type": "error",
                "code": "runtime_unavailable",
                "message": "unable to initialize local model runtime",
            }
        )
        return 2

    token_delay = max(0.0, float(os.environ.get("TOKEN_PLACE_SIDECAR_TOKEN_DELAY_SECONDS", "0")))

    try:
        completion = llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=runtime_config.get("model.max_tokens", 512),
            temperature=runtime_config.get("model.temperature", 0.7),
            top_p=runtime_config.get("model.top_p", 0.9),
            stop=runtime_config.get("model.stop_tokens", []),
            stream=True,
        )

        _emit({"type": "started"})
        for token_text in _stream_completion_tokens(completion):
            if _cancel_requested():
                _emit({"type": "canceled"})
                return 0
            _emit({"type": "token", "text": token_text})
            if token_delay:
                time.sleep(token_delay)

        if _cancel_requested():
            _emit({"type": "canceled"})
            return 0

        _emit({"type": "done"})
        return 0
    except Exception as exc:  # pragma: no cover - defensive runtime failures
        _emit(
            {
                "type": "error",
                "code": "inference_failed",
                "message": f"inference bridge failed: {exc}",
            }
        )
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="token.place desktop inference bridge")
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()

    return run_inference(model_path=args.model, mode=args.mode, prompt=args.prompt)


if __name__ == "__main__":
    raise SystemExit(main())
