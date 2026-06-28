"""Central model profile metadata for token.place API v1 runtimes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


CANONICAL_LAUNCH_MODEL_ID = "llama-3.1-8b-instruct"
DEFAULT_PROFILE_ID = "llama-3.1-8b-q4-k-m"


@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    api_model_id: str
    display_name: str
    description: str
    owner: str
    provider: str
    source_model: str
    parameters: str
    quantization: str
    license: str
    gguf_repo: str
    filename: str
    download_url: str
    canonical_family_url: str
    native_context_tokens: int
    maximum_validated_context_tokens: int
    default_context_tokens: int
    supported_context_tiers: List[str]
    chat_template_policy: str
    thinking_mode: str
    generation_defaults: Dict[str, Any]
    aliases: List[str]
    rope_scaling_policy: Optional[Dict[str, Any]] = None
    public_catalog: bool = False
    runnable: bool = False
    chat_format: str = "llama-3"

    def to_dict(self) -> Dict[str, Any]:
        return deepcopy(asdict(self))

    def to_catalog_entry(self) -> Dict[str, Any]:
        entry = {
            "id": self.api_model_id,
            "name": self.display_name,
            "description": self.description,
            "owner": self.owner,
            "owned_by": self.owner,
            "provider": self.provider,
            "source_model": self.source_model,
            "parameters": self.parameters,
            "quantization": self.quantization,
            "license": self.license,
            "gguf_repo": self.gguf_repo,
            "context_length": self.default_context_tokens,
            "native_context_tokens": self.native_context_tokens,
            "maximum_validated_context_tokens": self.maximum_validated_context_tokens,
            "supported_context_tiers": list(self.supported_context_tiers),
            "chat_template_policy": self.chat_template_policy,
            "thinking_mode": self.thinking_mode,
            "url": self.download_url,
            "file_name": self.filename,
            "profile_id": self.profile_id,
            "runnable": self.runnable,
            "adapters": [],
        }
        if self.rope_scaling_policy is not None:
            entry["rope_scaling_policy"] = deepcopy(self.rope_scaling_policy)
        return entry


LLAMA_3_1_8B_Q4_K_M = ModelProfile(
    profile_id=DEFAULT_PROFILE_ID,
    api_model_id=CANONICAL_LAUNCH_MODEL_ID,
    display_name="Meta Llama 3.1 8B Instruct",
    description=(
        "Meta's July 2024 refresh of the 8B instruction-tuned model using the "
        "Q4_K_M quantisation that comfortably fits within a 24 GB RTX 4090."
    ),
    owner="Meta",
    provider="meta",
    source_model="meta-llama/Llama-3.1-8B-Instruct",
    parameters="8B",
    quantization="Q4_K_M",
    license="llama3.1",
    gguf_repo="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
    filename="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
    download_url=(
        "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/"
        "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
    ),
    canonical_family_url="https://huggingface.co/meta-llama/Meta-Llama-3-8B",
    native_context_tokens=8192,
    maximum_validated_context_tokens=8192,
    default_context_tokens=8192,
    supported_context_tiers=["8k-fast"],
    chat_template_policy="llama-cpp-chat-format",
    thinking_mode="not-applicable",
    generation_defaults={"temperature": 0.7, "max_tokens": 1000},
    aliases=["llama-3-8b-instruct", "gpt-3.5-turbo", "gpt-5-chat-latest"],
    public_catalog=True,
    runnable=True,
    chat_format="llama-3",
)


QWEN3_8B_Q4_K_M = ModelProfile(
    profile_id="qwen3-8b-q4-k-m",
    api_model_id="qwen3-8b-instruct",
    display_name="Qwen3 8B Instruct",
    description=(
        "Internal scaffold for Qwen3 8B Q4_K_M API v1 support. Runtime loading, "
        "chat-template behavior, and extended-context YaRN/RoPE settings are not active yet."
    ),
    owner="Qwen",
    provider="qwen",
    source_model="Qwen/Qwen3-8B",
    parameters="8.2B",
    quantization="Q4_K_M",
    license="apache-2.0",
    gguf_repo="Qwen/Qwen3-8B-GGUF",
    filename="Qwen3-8B-Q4_K_M.gguf",
    download_url="https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
    canonical_family_url="https://huggingface.co/Qwen/Qwen3-8B",
    native_context_tokens=32768,
    maximum_validated_context_tokens=131072,
    default_context_tokens=8192,
    supported_context_tiers=["8k-fast", "64k-full"],
    chat_template_policy="gguf-jinja",
    thinking_mode="disabled",
    generation_defaults={"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
    aliases=[],
    rope_scaling_policy={
        "type": "yarn",
        "status": "planned",
        "native_context_tokens": 32768,
        "target_context_tokens": 65536,
        "factor": 2.0,
        "required_for_tier": "64k-full",
    },
    public_catalog=False,
    runnable=False,
    chat_format="chatml",
)


MODEL_PROFILES: Dict[str, ModelProfile] = {
    LLAMA_3_1_8B_Q4_K_M.profile_id: LLAMA_3_1_8B_Q4_K_M,
    QWEN3_8B_Q4_K_M.profile_id: QWEN3_8B_Q4_K_M,
}

API_MODEL_ID_TO_PROFILE_ID: Dict[str, str] = {
    profile.api_model_id: profile.profile_id for profile in MODEL_PROFILES.values()
}

MODEL_ALIASES: Dict[str, str] = {
    alias: LLAMA_3_1_8B_Q4_K_M.api_model_id for alias in LLAMA_3_1_8B_Q4_K_M.aliases
}


def get_model_profile(profile_id: str = DEFAULT_PROFILE_ID) -> ModelProfile:
    return MODEL_PROFILES[profile_id]


def get_default_model_profile() -> ModelProfile:
    return get_model_profile(DEFAULT_PROFILE_ID)


def get_public_catalog_profiles() -> List[ModelProfile]:
    return [profile for profile in MODEL_PROFILES.values() if profile.public_catalog]


def resolve_profile_id(profile_id: Optional[str] = None, api_model_id: Optional[str] = None) -> str:
    if profile_id and profile_id in MODEL_PROFILES:
        return profile_id
    if api_model_id and api_model_id in API_MODEL_ID_TO_PROFILE_ID:
        return API_MODEL_ID_TO_PROFILE_ID[api_model_id]
    return DEFAULT_PROFILE_ID
