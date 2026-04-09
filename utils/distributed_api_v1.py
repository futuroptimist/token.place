"""Shared helpers for distributed API v1 compute request/response handling."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List


def build_chat_completion_response(
    *,
    model: str,
    assistant_message: Dict[str, Any],
    response_id: str | None = None,
) -> Dict[str, Any]:
    """Build an OpenAI-compatible chat completion response envelope."""

    return {
        "id": response_id or f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": assistant_message.get("role", "assistant"),
                    "content": assistant_message.get("content", ""),
                    **(
                        {"tool_calls": assistant_message.get("tool_calls")}
                        if assistant_message.get("tool_calls")
                        else {}
                    ),
                },
                "finish_reason": "tool_calls" if assistant_message.get("tool_calls") else "stop",
            }
        ],
    }


def extract_assistant_message(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the last assistant message from a generated chat transcript."""

    if not messages:
        return {"role": "assistant", "content": ""}

    candidate = messages[-1]
    if isinstance(candidate, dict):
        return candidate
    return {"role": "assistant", "content": str(candidate)}


def completion_prompt_to_messages(prompt: Any) -> List[Dict[str, str]]:
    """Normalize v1 completion prompt input into chat message format."""

    if isinstance(prompt, list):
        prompt_text = "".join(str(part) for part in prompt)
    else:
        prompt_text = "" if prompt is None else str(prompt)
    return [{"role": "user", "content": prompt_text}]
