"""Typed configuration schema and defaults for :mod:`token.place`.

This module centralises the structure of the application's configuration tree.  It
provides ``TypedDict`` based views for each section together with the default
configuration payload and the environment specific overrides that are merged at
runtime.  Keeping this information in a dedicated module makes it easier to
reason about the available configuration keys and enables static analysers to
offer better auto-completion for developers.
"""

from __future__ import annotations

from typing import Dict, List, Optional, TypedDict

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
    default_model: str
    fallback_model: str
    temperature: float
    max_tokens: int
    use_mock: bool
    filename: str
    url: str
    context_size: int
    chat_format: str
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
        "server_url": "http://localhost:5000",
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
        "relay_url": "http://localhost:5000",
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
        "default_model": "gpt-5-chat-latest",
        "fallback_model": "gpt-5-chat-latest",
        "temperature": 0.7,
        "max_tokens": 1000,
        "use_mock": False,
        "filename": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        "url": (
            "https://huggingface.co/bartowski/Meta-Llama-3.1-8B-Instruct-GGUF/resolve/main/"
            "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
        ),
        "context_size": 8192,
        "chat_format": "llama-3",
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
