"""Central model profile catalog for API v1 runtime artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional


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
    aliases: List[str] = field(default_factory=list)
    rope_scaling_policy: Optional[Dict[str, Any]] = None
    public_catalog: bool = True
    runtime_status: str = "runnable"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


LLAMA_3_1_8B_PROFILE = ModelProfile(
    profile_id="llama-3.1-8b-q4-k-m",
    api_model_id="llama-3.1-8b-instruct",
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
    chat_template_policy="llama-3",
    thinking_mode="unsupported",
    generation_defaults={},
    aliases=["llama-3-8b-instruct", "gpt-3.5-turbo", "gpt-5-chat-latest"],
)


QWEN3_8B_PROFILE = ModelProfile(
    profile_id="qwen3-8b-q4-k-m",
    api_model_id="qwen3-8b-instruct",
    display_name="Qwen3 8B Instruct",
    description=(
        "Internal, non-default Qwen3 8B Q4_K_M profile metadata staged for future "
        "API v1 runtime enablement."
    ),
    owner="Qwen",
    provider="qwen",
    source_model="Qwen/Qwen3-8B",
    parameters="8.2B",
    quantization="Q4_K_M",
    license="apache-2.0",
    gguf_repo="Qwen/Qwen3-8B-GGUF",
    filename="Qwen3-8B-Q4_K_M.gguf",
    download_url=(
        "https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/"
        "Qwen3-8B-Q4_K_M.gguf"
    ),
    canonical_family_url="https://huggingface.co/Qwen/Qwen3-8B",
    native_context_tokens=32768,
    maximum_validated_context_tokens=131072,
    default_context_tokens=65536,
    supported_context_tiers=["8k-fast", "64k-full"],
    chat_template_policy="gguf-jinja",
    thinking_mode="disabled",
    generation_defaults={"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0},
    aliases=[],
    rope_scaling_policy={"type": "yarn", "factor": 2.0, "base_context_tokens": 32768, "target_context_tokens": 65536},
    public_catalog=False,
    runtime_status="internal_metadata_only",
)


DEFAULT_PROFILE_ID = LLAMA_3_1_8B_PROFILE.profile_id
CANONICAL_LAUNCH_MODEL_ID = LLAMA_3_1_8B_PROFILE.api_model_id

MODEL_PROFILES: Mapping[str, ModelProfile] = {
    LLAMA_3_1_8B_PROFILE.profile_id: LLAMA_3_1_8B_PROFILE,
    QWEN3_8B_PROFILE.profile_id: QWEN3_8B_PROFILE,
}

MODEL_ALIASES: Dict[str, str] = {
    alias: LLAMA_3_1_8B_PROFILE.api_model_id for alias in LLAMA_3_1_8B_PROFILE.aliases
}


def get_model_profile(profile_id: str) -> Optional[ModelProfile]:
    return MODEL_PROFILES.get(profile_id)


def get_default_model_profile() -> ModelProfile:
    return LLAMA_3_1_8B_PROFILE


def get_active_model_profile(config: Any | None = None) -> ModelProfile:
    if config is None:
        return get_default_model_profile()
    profile_id = config.get("model.profile_id", DEFAULT_PROFILE_ID)
    return MODEL_PROFILES.get(profile_id, get_default_model_profile())


def public_api_v1_profiles() -> List[ModelProfile]:
    return [profile for profile in MODEL_PROFILES.values() if profile.public_catalog]
