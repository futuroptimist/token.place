"""Test-only llama_cpp shim.

This module must never be imported in real runtime flows. Opt in explicitly with
TOKENPLACE_ENABLE_REPO_LLAMA_CPP_STUB=1 for narrowly-scoped tests.
"""

import os

if os.getenv("TOKENPLACE_ENABLE_REPO_LLAMA_CPP_STUB") != "1":
    raise RuntimeError(
        "Repo-local llama_cpp.py stub was imported. This shadows llama-cpp-python; "
        "remove repo root from import precedence or install/use the real package."
    )


class Llama:
    """Minimal stub of llama_cpp.Llama for explicit test opt-in only."""

    def __init__(self, *args, **kwargs):
        pass

    def create_chat_completion(self, *args, **kwargs):
        return {"choices": [{"message": {"role": "assistant", "content": "stub"}}]}
