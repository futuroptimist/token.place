"""Centralized API v1 model profile metadata."""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any, Dict, Iterable, Optional, TypedDict

logger = logging.getLogger(__name__)


class _ModelProfileRequired(TypedDict):
    profile_id: str
    api_model_id: str
    display_name: str
    description: str
    owner: str
    provider: str
    source_model: str
    parameters: str
    quantization: str
    filename: str
    download_url: str
    canonical_family_url: str
    native_context_tokens: int
    maximum_validated_context_tokens: int
    default_context_tokens: int
    supported_context_tiers: list[str]
    chat_template_policy: str
    thinking_mode: str
    generation_defaults: Dict[str, Any]
    aliases: list[str]
    public_catalog: bool
    runnable: bool


class ModelProfile(_ModelProfileRequired, total=False):
    license: str
    gguf_repo: str
    rope_scaling_policy: Optional[Dict[str, Any]]


LLAMA_3_1_8B_PROFILE_ID = "llama-3.1-8b-q4-k-m"
CANONICAL_API_V1_MODEL_ID = "llama-3.1-8b-instruct"
QWEN3_8B_PROFILE_ID = "qwen3-8b-q4-k-m"
QWEN3_API_MODEL_ID = "qwen3-8b-instruct"

LLAMA_DOWNLOAD_URL = (
    "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/"
    "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
)
LLAMA_FILENAME = "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
LLAMA_FAMILY_URL = "https://huggingface.co/meta-llama/Meta-Llama-3-8B"

MODEL_PROFILES: Dict[str, ModelProfile] = {
    LLAMA_3_1_8B_PROFILE_ID: {
        "profile_id": LLAMA_3_1_8B_PROFILE_ID,
        "api_model_id": CANONICAL_API_V1_MODEL_ID,
        "display_name": "Meta Llama 3.1 8B Instruct",
        "description": (
            "Meta's July 2024 refresh of the 8B instruction-tuned model using the "
            "Q4_K_M quantisation that comfortably fits within a 24 GB RTX 4090."
        ),
        "owner": "Meta",
        "provider": "meta",
        "source_model": "meta-llama/Llama-3.1-8B-Instruct",
        "parameters": "8B",
        "quantization": "Q4_K_M",
        "license": "llama3.1",
        "gguf_repo": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "filename": LLAMA_FILENAME,
        "download_url": LLAMA_DOWNLOAD_URL,
        "canonical_family_url": LLAMA_FAMILY_URL,
        "native_context_tokens": 8192,
        "maximum_validated_context_tokens": 8192,
        "default_context_tokens": 8192,
        "supported_context_tiers": ["8k-fast"],
        "chat_template_policy": "llama-3",
        "thinking_mode": "not-applicable",
        "generation_defaults": {},
        "aliases": ["llama-3-8b-instruct", "gpt-3.5-turbo", "gpt-5-chat-latest"],
        "rope_scaling_policy": None,
        "public_catalog": True,
        "runnable": True,
    },
    QWEN3_8B_PROFILE_ID: {
        "profile_id": QWEN3_8B_PROFILE_ID,
        "api_model_id": QWEN3_API_MODEL_ID,
        "display_name": "Qwen3 8B Instruct",
        "description": "Internal non-default profile metadata for future Qwen3 8B API v1 support.",
        "owner": "Qwen",
        "provider": "qwen",
        "source_model": "Qwen/Qwen3-8B",
        "parameters": "8.2B",
        "quantization": "Q4_K_M",
        "license": "apache-2.0",
        "gguf_repo": "Qwen/Qwen3-8B-GGUF",
        "filename": "Qwen3-8B-Q4_K_M.gguf",
        "download_url": "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
        "canonical_family_url": "https://huggingface.co/Qwen/Qwen3-8B",
        "native_context_tokens": 32768,
        "maximum_validated_context_tokens": 131072,
        "default_context_tokens": 8192,
        "supported_context_tiers": ["8k-fast", "64k-full"],
        "chat_template_policy": "gguf-jinja",
        "thinking_mode": "disabled",
        "generation_defaults": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0},
        "aliases": [],
        "rope_scaling_policy": {"type": "yarn", "required_for_tier": "64k-full", "factor": 2.0, "original_context_tokens": 32768},
        "public_catalog": False,
        "runnable": False,
    },
}


def get_model_profile(profile_id: str) -> Optional[ModelProfile]:
    profile = MODEL_PROFILES.get(profile_id)
    return deepcopy(profile) if profile else None


def get_default_model_profile() -> ModelProfile:
    profile = get_model_profile(LLAMA_3_1_8B_PROFILE_ID)
    if profile is None:
        raise RuntimeError(
            f"Default model profile '{LLAMA_3_1_8B_PROFILE_ID}' not found in MODEL_PROFILES. "
            "Ensure the Llama profile has not been removed."
        )
    return profile


def iter_model_profiles(*, public_only: bool = False) -> Iterable[ModelProfile]:
    for profile in MODEL_PROFILES.values():
        if public_only and not profile.get("public_catalog", False):
            continue
        yield deepcopy(profile)


def resolve_profile_id(profile_id: Optional[str], api_model_id: Optional[str] = None) -> str:
    if profile_id and profile_id in MODEL_PROFILES:
        return profile_id
    if api_model_id:
        for candidate_id, profile in MODEL_PROFILES.items():
            if profile["api_model_id"] == api_model_id:
                return candidate_id
    if profile_id or api_model_id:
        logger.warning(
            "Unknown model profile selection (profile_id=%r, api_model_id=%r); falling back to %s",
            profile_id,
            api_model_id,
            LLAMA_3_1_8B_PROFILE_ID,
        )
    return LLAMA_3_1_8B_PROFILE_ID


def build_model_aliases() -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for profile in MODEL_PROFILES.values():
        target = profile["api_model_id"]
        for alias in profile.get("aliases", []):
            aliases[alias] = target
    return aliases
