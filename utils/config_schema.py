"""Typed configuration schema and defaults for :mod:`token.place`.

This module centralises the structure of the application's configuration tree.  It
provides ``TypedDict`` based views for each section together with the default
configuration payload and the environment specific overrides that are merged at
runtime.  Keeping this information in a dedicated module makes it easier to
reason about the available configuration keys and enables static analysers to
offer better auto-completion for developers.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from utils.llm.model_profiles import LLAMA_3_1_8B_Q4_K_M_PROFILE

class ServerSettings(TypedDict, total=False):
    host: str
    port: int
    debug: bool
    workers: int
    timeout: int
    base_url: str


class RelaySettings(TypedDict, total=False):
    host: str
    port: int
    server_url: str
    server_pool: List[str]
    server_pool_secondary: List[str]
    workers: int
    additional_servers: List[str]
    server_registration_token: Optional[str]
    cluster_only: bool
    cloudflare_fallback_urls: List[str]


class APISettings(TypedDict, total=False):
    host: str
    port: int
    relay_url: str
    cors_origins: List[str]


class SecuritySettings(TypedDict, total=False):
    encryption_enabled: bool
    key_size: int
    key_expiry_days: int


class PathsSettings(TypedDict, total=False):
    data_dir: str
    models_dir: str
    logs_dir: str
    cache_dir: str
    keys_dir: str
    config_dir: str


class ModelSettings(TypedDict, total=False):
    profile_id: str
    api_model_id: str
    default_model: str
    fallback_model: str
    temperature: float
    max_tokens: int
    use_mock: bool
    filename: str
    url: str
    canonical_family_url: str
    context_size: int
    chat_format: str
    chat_template_policy: str
    thinking_mode: str
    native_context_tokens: int
    maximum_validated_context_tokens: int
    supported_context_tiers: List[str]
    rope_scaling_policy: Optional[Dict[str, Any]]
    download_chunk_size_mb: int


class ConstantsSettings(TypedDict, total=False):
    KB: int
    MB: int
    GB: int


class AppConfig(TypedDict):
    server: ServerSettings
    relay: RelaySettings
    api: APISettings
    security: SecuritySettings
    paths: PathsSettings
    model: ModelSettings
    constants: ConstantsSettings


class PartialAppConfig(TypedDict, total=False):
    server: ServerSettings
    relay: RelaySettings
    api: APISettings
    security: SecuritySettings
    paths: PathsSettings
    model: ModelSettings
    constants: ConstantsSettings


# Default configuration values used to seed :class:`~config.Config`.
DEFAULT_CONFIG: AppConfig = {
    "server": {
        "host": "127.0.0.1",
        "port": 5000,
        "debug": False,
        "workers": 4,
        "timeout": 30,
        "base_url": "http://localhost",
    },
    "relay": {
        "host": "127.0.0.1",
        "port": 5000,
        "server_url": "https://token.place",
        "server_pool": [],
        "server_pool_secondary": [],
        "workers": 2,
        "additional_servers": [],
        "server_registration_token": None,
        "cluster_only": False,
        "cloudflare_fallback_urls": [],
    },
    "api": {
        "host": "127.0.0.1",
        "port": 3000,
        "relay_url": "https://token.place",
        "cors_origins": ["*"],
    },
    "security": {
        "encryption_enabled": True,
        "key_size": 2048,
        "key_expiry_days": 30,
    },
    "paths": {
        "data_dir": "",
        "models_dir": "",
        "logs_dir": "",
        "cache_dir": "",
        "keys_dir": "",
        "config_dir": "",
    },
    "model": {
        "profile_id": LLAMA_3_1_8B_Q4_K_M_PROFILE.profile_id,
        "api_model_id": LLAMA_3_1_8B_Q4_K_M_PROFILE.api_model_id,
        "default_model": "gpt-5-chat-latest",
        "fallback_model": "gpt-5-chat-latest",
        "temperature": 0.7,
        "max_tokens": 1000,
        "use_mock": False,
        "filename": LLAMA_3_1_8B_Q4_K_M_PROFILE.filename,
        "url": LLAMA_3_1_8B_Q4_K_M_PROFILE.download_url,
        "canonical_family_url": LLAMA_3_1_8B_Q4_K_M_PROFILE.canonical_family_url,
        "context_size": LLAMA_3_1_8B_Q4_K_M_PROFILE.default_context_tokens,
        "chat_format": "llama-3",
        "chat_template_policy": LLAMA_3_1_8B_Q4_K_M_PROFILE.chat_template_policy,
        "thinking_mode": LLAMA_3_1_8B_Q4_K_M_PROFILE.thinking_mode,
        "native_context_tokens": LLAMA_3_1_8B_Q4_K_M_PROFILE.native_context_tokens,
        "maximum_validated_context_tokens": LLAMA_3_1_8B_Q4_K_M_PROFILE.maximum_validated_context_tokens,
        "supported_context_tiers": LLAMA_3_1_8B_Q4_K_M_PROFILE.supported_context_tiers,
        "rope_scaling_policy": LLAMA_3_1_8B_Q4_K_M_PROFILE.rope_scaling_policy,
        "download_chunk_size_mb": 10,
    },
    "constants": {
        "KB": 1024,
        "MB": 1024 * 1024,
        "GB": 1024 * 1024 * 1024,
    },
}


ENV_OVERRIDES: Dict[str, PartialAppConfig] = {
    "development": {
        "server": {
            "debug": True,
        },
        "security": {
            "encryption_enabled": True,
        },
    },
    "testing": {
        "server": {
            "debug": True,
            "port": 8001,
        },
        "relay": {
            "port": 5001,
            "server_url": "http://localhost:8001",
        },
        "api": {
            "port": 3001,
            "relay_url": "http://localhost:5001",
        },
        "security": {
            "encryption_enabled": True,
            "key_size": 1024,
        },
    },
    "production": {
        "server": {
            "debug": False,
            "workers": 8,
        },
        "security": {
            "encryption_enabled": True,
        },
    },
}
