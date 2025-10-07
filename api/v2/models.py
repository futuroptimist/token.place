"""Model catalogue for token.place API v2."""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

# The v2 catalogue focuses on models that are practical to host on a single
# RTX 4090 class GPU using the referenced quantisations. Each entry captures the
# quantised artifact token.place can serve alongside descriptive metadata.
AVAILABLE_MODELS: List[Dict[str, Any]] = [
    {
        "id": "llama-3-8b-instruct",
        "name": "Meta Llama 3 8B Instruct",
        "description": (
            "Meta's 8B instruction-tuned release with Q4_K_M quantisation; the artifact "
            "is roughly 4.9 GB, making it straightforward to deploy on a 24 GB GPU."
        ),
        "parameters": "8B",
        "quantization": "Q4_K_M",
        "context_length": 8192,
        "url": (
            "https://huggingface.co/bartowski/Meta-Llama-3-8B-Instruct-GGUF/resolve/main/"
            "Meta-Llama-3-8B-Instruct-Q4_K_M.gguf"
        ),
        "file_name": "Meta-Llama-3-8B-Instruct-Q4_K_M.gguf",
        "adapters": [
            {
                "id": "llama-3-8b-instruct:alignment",
                "name": "Meta Llama 3 8B Alignment Assistant",
                "description": (
                    "Alignment-focused profile that layers a safety charter on the base "
                    "model while reusing the same 4.9 GB quantised weights."
                ),
                "instructions": (
                    "You are the alignment-focused variant of Meta Llama 3 8B. Follow the "
                    "provided safety charter to remain helpful, honest, harmless, and to call "
                    "out uncertain answers."
                ),
                "share_base": True,
            }
        ],
    },
    {
        "id": "gpt-oss-20b",
        "name": "OpenAI gpt-oss 20B",
        "description": (
            "OpenAI's open-weight MoE model; the unsloth GGUF notes the 20B variant runs "
            "within 16 GB of memory, keeping it inside RTX 4090 limits while delivering "
            "strong coding performance."
        ),
        "parameters": "20B (MoE)",
        "quantization": "MXFP4/BF16",
        "context_length": 65536,
        "url": (
            "https://huggingface.co/unsloth/gpt-oss-20b-GGUF/resolve/main/"
            "gpt-oss-20b-Q4_K_M.gguf"
        ),
        "file_name": "gpt-oss-20b-Q4_K_M.gguf",
    },
    {
        "id": "mistral-7b-instruct",
        "name": "Mistral 7B Instruct v0.2",
        "description": (
            "Balanced 7B chat model with a recommended Q4_K_M build requiring around "
            "6.9 GB of VRAM when fully offloaded."
        ),
        "parameters": "7B",
        "quantization": "Q4_K_M",
        "context_length": 8192,
        "url": (
            "https://huggingface.co/TheBloke/Mistral-7B-Instruct-v0.2-GGUF/resolve/main/"
            "mistral-7b-instruct-v0.2.Q4_K_M.gguf"
        ),
        "file_name": "mistral-7b-instruct-v0.2.Q4_K_M.gguf",
    },
    {
        "id": "mixtral-8x7b-instruct",
        "name": "Mixtral 8x7B Instruct",
        "description": (
            "Mixture-of-experts release; the Q3_K_M build is ~20 GB with a 22.9 GB RAM "
            "footprint, fitting comfortably on a 24 GB card while retaining MoE quality."
        ),
        "parameters": "8x7B MoE",
        "quantization": "Q3_K_M",
        "context_length": 32768,
        "url": (
            "https://huggingface.co/TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF/resolve/main/"
            "mixtral-8x7b-instruct-v0.1.Q3_K_M.gguf"
        ),
        "file_name": "mixtral-8x7b-instruct-v0.1.Q3_K_M.gguf",
    },
    {
        "id": "phi-3-mini-4k-instruct",
        "name": "Phi-3 Mini 4K Instruct",
        "description": (
            "Compact Microsoft Phi-3 variant; the Q4_K_M weight is ~2.4 GB, ideal for "
            "tooling workloads on consumer GPUs."
        ),
        "parameters": "3.8B",
        "quantization": "Q4_K_M",
        "context_length": 4096,
        "url": (
            "https://huggingface.co/bartowski/Phi-3-mini-4k-instruct-GGUF/resolve/main/"
            "Phi-3-mini-4k-instruct-Q4_K_M.gguf"
        ),
        "file_name": "Phi-3-mini-4k-instruct-Q4_K_M.gguf",
    },
    {
        "id": "mistral-nemo-instruct",
        "name": "Mistral Nemo Instruct 2407",
        "description": (
            "Frontier 12B collaboration between Mistral and NVIDIA; the Q4_K_M quant "
            "occupies roughly 7.5 GB enabling rich multimodal-style responses on a 4090."
        ),
        "parameters": "12B",
        "quantization": "Q4_K_M",
        "context_length": 32768,
        "url": (
            "https://huggingface.co/bartowski/Mistral-Nemo-Instruct-2407-GGUF/resolve/main/"
            "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf"
        ),
        "file_name": "Mistral-Nemo-Instruct-2407-Q4_K_M.gguf",
    },
    {
        "id": "qwen2.5-7b-instruct",
        "name": "Qwen2.5 7B Instruct",
        "description": (
            "Qwen's 7B general assistant; the recommended Q4_K_M build is 4.68 GB, "
            "keeping latency low while supporting long contexts."
        ),
        "parameters": "7B",
        "quantization": "Q4_K_M",
        "context_length": 131072,
        "url": (
            "https://huggingface.co/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/"
            "Qwen2.5-7B-Instruct-Q4_K_M.gguf"
        ),
        "file_name": "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
    },
    {
        "id": "qwen2.5-coder-7b-instruct",
        "name": "Qwen2.5 Coder 7B Instruct",
        "description": (
            "Code-specialised Qwen2.5 variant with the same lightweight Q4_K_M footprint "
            "(~4.68 GB) tuned for IDE copilots."
        ),
        "parameters": "7B",
        "quantization": "Q4_K_M",
        "context_length": 131072,
        "url": (
            "https://huggingface.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/"
            "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf"
        ),
        "file_name": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
    },
    {
        "id": "gemma-2-9b-it",
        "name": "Gemma 2 9B IT",
        "description": (
            "Google's Gemma 2 chat model; the Q4_K_M GGUF is ~5.8 GB enabling fast "
            "inference and strong multilingual coverage."
        ),
        "parameters": "9B",
        "quantization": "Q4_K_M",
        "context_length": 8192,
        "url": (
            "https://huggingface.co/bartowski/gemma-2-9b-it-GGUF/resolve/main/"
            "gemma-2-9b-it-Q4_K_M.gguf"
        ),
        "file_name": "gemma-2-9b-it-Q4_K_M.gguf",
    },
    {
        "id": "codegemma-7b",
        "name": "CodeGemma 7B",
        "description": (
            "Gemma's code-specialised sibling; the recommended Q4_K_M build is 5.3 GB, "
            "providing coding assistance without saturating GPU memory."
        ),
        "parameters": "7B",
        "quantization": "Q4_K_M",
        "context_length": 8192,
        "url": (
            "https://huggingface.co/bartowski/codegemma-7b-GGUF/resolve/main/"
            "codegemma-7b-Q4_K_M.gguf"
        ),
        "file_name": "codegemma-7b-Q4_K_M.gguf",
    },
    {
        "id": "smollm2-1.7b-instruct",
        "name": "SmolLM2 1.7B Instruct",
        "description": (
            "Ultra-light 1.7B assistant from Hugging Face; the Q4_K_M artifact is about "
            "1.1 GB, perfect for latency-sensitive tasks."
        ),
        "parameters": "1.7B",
        "quantization": "Q4_K_M",
        "context_length": 8192,
        "url": (
            "https://huggingface.co/bartowski/SmolLM2-1.7B-Instruct-GGUF/resolve/main/"
            "SmolLM2-1.7B-Instruct-Q4_K_M.gguf"
        ),
        "file_name": "SmolLM2-1.7B-Instruct-Q4_K_M.gguf",
    },
]


def _iter_model_entries() -> Iterable[Dict[str, Any]]:
    """Yield model entries, expanding adapter metadata if present."""
    for base in AVAILABLE_MODELS:
        base_entry = {key: value for key, value in base.items() if key != "adapters"}
        base_entry["base_model_id"] = base["id"]
        yield base_entry

        for adapter in base.get("adapters", []):
            derived = {key: value for key, value in base.items() if key != "adapters"}
            derived["id"] = adapter["id"]
            derived["name"] = adapter.get("name", base_entry["name"])
            derived["description"] = adapter.get("description", base_entry["description"])
            derived["file_name"] = adapter.get("file_name", base_entry.get("file_name"))
            derived["parameters"] = adapter.get("parameters", base_entry.get("parameters"))
            derived["quantization"] = adapter.get("quantization", base_entry.get("quantization"))
            derived["context_length"] = adapter.get("context_length", base_entry.get("context_length"))
            derived["url"] = adapter.get("url", base_entry.get("url"))
            derived["base_model_id"] = base["id"]
            derived["adapter"] = {
                "id": adapter["id"],
                "instructions": adapter.get("instructions"),
                "prompt_template": adapter.get("prompt_template"),
                "share_base": adapter.get("share_base", False),
            }
            yield derived


def get_models_info() -> List[Dict[str, Any]]:
    """Return flattened metadata for API v2 consumers."""
    return list(_iter_model_entries())
