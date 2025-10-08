"""
Configuration file for token.place application
Values are loaded from environment variables with sensible defaults
"""

import os
import sys
import platform
import json
import logging
import copy
from typing import Dict, Any, List, Optional
from pathlib import Path

# Global constants for platform detection
IS_WINDOWS = platform.system().lower() == "windows"
IS_MACOS = platform.system().lower() == "darwin"
IS_LINUX = platform.system().lower() == "linux"

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('config')

# Default configuration values
DEFAULT_CONFIG = {
    # Server settings
    'server': {
        'host': '127.0.0.1',
        'port': 5000,
        'debug': False,
        'workers': 4,
        'timeout': 30,
        'base_url': 'http://localhost',
    },

    # Relay settings
    'relay': {
        'host': '127.0.0.1',
        'port': 5000,
        'server_url': 'http://localhost:5000',
        'server_pool': [],
        'server_pool_secondary': [],
        'workers': 2,
        'additional_servers': [],
        'server_registration_token': None,
        'cluster_only': False,
    },

    # API settings
    'api': {
        'host': '127.0.0.1',
        'port': 3000,
        'relay_url': 'http://localhost:5000',
        'cors_origins': ['*'],
    },

    # Security settings
    'security': {
        'encryption_enabled': True,
        'key_size': 2048,
        'key_expiry_days': 30,
    },

    # Data paths (these will be overridden based on platform)
    'paths': {
        'data_dir': '',  # Will be set based on platform
        'models_dir': '', # Will be set based on platform
        'logs_dir': '',  # Will be set based on platform
        'cache_dir': '',  # Will be set based on platform
        'keys_dir': '',  # Will be set based on platform
    },

    # Model settings
    'model': {
        'default_model': 'gpt-3.5-turbo',
        'fallback_model': 'gpt-3.5-turbo',
        'temperature': 0.7,
        'max_tokens': 1000,
        'use_mock': False,
        'filename': 'llama-3-8b-instruct.Q4_K_M.gguf',
        'url': 'https://huggingface.co/TheBloke/Llama-3-8B-Instruct-GGUF/resolve/main/llama-3-8b-instruct.Q4_K_M.gguf',
        'context_size': 8192,
        'chat_format': 'llama-3',
        'download_chunk_size_mb': 10,
    },

    # Helper constants
    'constants': {
        'KB': 1024,
        'MB': 1024 * 1024,
        'GB': 1024 * 1024 * 1024,
    },
}

# Environment-specific overrides
ENV_OVERRIDES = {
    'development': {
        'server': {
            'debug': True,
        },
        'security': {
            'encryption_enabled': True,
        },
    },
    'testing': {
        'server': {
            'debug': True,
            'port': 8001,
        },
        'relay': {
            'port': 5001,
            'server_url': 'http://localhost:8001',
        },
        'api': {
            'port': 3001,
            'relay_url': 'http://localhost:5001',
        },
        'security': {
            'encryption_enabled': True,
            'key_size': 1024,  # Smaller keys for faster testing
        },
    },
    'production': {
        'server': {
            'debug': False,
            'workers': 8,
            'host': os.environ.get('PROD_SERVER_HOST', '127.0.0.1'),
        },
        'relay': {
            'host': os.environ.get('PROD_RELAY_HOST', '127.0.0.1'),
        },
        'api': {
            'host': os.environ.get('PROD_API_HOST', '127.0.0.1'),
        },
        'security': {
            'encryption_enabled': True,
        },
    },
}

class Config:
    """Configuration manager for token.place"""

    def __init__(self, env: Optional[str] = None, config_path: Optional[str] = None):
        """
        Initialize configuration with the specified environment.
        If env is None, it tries to read from TOKEN_PLACE_ENV environment variable,
        defaulting to 'development' if not set.
        """
        # Determine the environment
        self.env = env or os.environ.get('TOKEN_PLACE_ENV', 'development')

        # Detect platform if not already set
        self.platform = os.environ.get('PLATFORM', platform.system().lower())

        # Load base configuration using a deep copy to avoid mutating DEFAULT_CONFIG
        self.config = copy.deepcopy(DEFAULT_CONFIG)

        # Apply environment-specific overrides
        if self.env in ENV_OVERRIDES:
            self._merge_configs(self.config, ENV_OVERRIDES[self.env])

        # Set platform-specific paths
        self._configure_platform_paths()

        # Load user configuration if it exists
        self.config_path = config_path or os.environ.get('TOKEN_PLACE_CONFIG')
        if self.config_path:
            self._load_user_config()

        # Apply late-binding environment overrides such as deployment-specific hosts.
        self._apply_runtime_env_overrides()

        logger.info(f"Configuration initialized for environment: {self.env}, platform: {self.platform}")

    def _configure_platform_paths(self):
        """Configure paths based on the detected platform"""
        # Import path handling here to avoid circular imports
        from utils.path_handling import (
            get_config_dir, ensure_dir_exists, get_app_data_dir,
            get_models_dir, get_logs_dir, get_cache_dir
        )

        config_dir = get_config_dir()

        # Ensure the config directory exists
        ensure_dir_exists(config_dir)

        app_data_dir = get_app_data_dir()
        models_dir = get_models_dir()
        logs_dir = get_logs_dir()
        cache_dir = get_cache_dir()
        keys_dir = config_dir / 'keys'

        # Update the paths in the config
        self.config['paths'].update({
            'data_dir': str(app_data_dir),
            'models_dir': str(models_dir),
            'logs_dir': str(logs_dir),
            'cache_dir': str(cache_dir),
            'keys_dir': str(keys_dir),
            'config_dir': str(config_dir),
        })

        # Ensure all directories exist
        for dir_path in [app_data_dir, models_dir, logs_dir, cache_dir, keys_dir]:
            ensure_dir_exists(dir_path)

    def _load_user_config(self):
        """Load user configuration file and merge with current config"""
        try:
            with open(self.config_path, 'r') as f:
                user_config = json.load(f)
                self._merge_configs(self.config, user_config)
                logger.info(f"Loaded user configuration from {self.config_path}")
        except FileNotFoundError:
            logger.warning(f"User configuration file not found: {self.config_path}")
        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON in user configuration file: {self.config_path}")
        except Exception as e:
            logger.error(f"Error loading user configuration: {str(e)}")

    def _merge_configs(self, base_config: Dict[str, Any], override_config: Dict[str, Any]):
        """
        Recursively merge the override_config into the base_config.
        """
        for key, value in override_config.items():
            if key in base_config and isinstance(base_config[key], dict) and isinstance(value, dict):
                self._merge_configs(base_config[key], value)
            else:
                base_config[key] = value

    def _apply_runtime_env_overrides(self) -> None:
        """Apply runtime environment variable overrides to the configuration tree."""

        relay_upstreams = self._gather_configured_relay_upstreams()
        if relay_upstreams:
            self.set('relay.server_pool', relay_upstreams)
            self.set('relay.server_url', relay_upstreams[0])

        token = os.environ.get('TOKEN_PLACE_RELAY_SERVER_TOKEN')
        if token:
            self.set('relay.server_registration_token', token.strip())

        cluster_only_env = os.environ.get('TOKEN_PLACE_RELAY_CLUSTER_ONLY')
        if cluster_only_env is not None:
            parsed_cluster_only = self._parse_bool(cluster_only_env)
            if parsed_cluster_only is not None:
                self.set('relay.cluster_only', parsed_cluster_only)
            elif cluster_only_env.strip():
                logger.warning(
                    "Invalid TOKEN_PLACE_RELAY_CLUSTER_ONLY value: %s",
                    cluster_only_env,
                )

        self._normalise_relay_server_pool()

    def _gather_configured_relay_upstreams(self) -> List[str]:
        """Collect relay upstream URLs from supported environment variables."""

        upstreams: List[str] = []

        raw_pool = os.environ.get('TOKEN_PLACE_RELAY_UPSTREAMS', '').strip()
        if raw_pool:
            upstreams.extend(self._parse_relay_upstreams(raw_pool))

        legacy_alias = self._normalise_url(os.environ.get('PERSONAL_GAMING_PC_URL', ''))
        if legacy_alias:
            upstreams.insert(0, legacy_alias)

        deduped: List[str] = []
        seen = set()
        for url in upstreams:
            if url and url not in seen:
                seen.add(url)
                deduped.append(url)

        return deduped

    def _parse_relay_upstreams(self, raw_value: str) -> List[str]:
        """Parse comma- or JSON-delimited upstream specifications."""

        candidates: List[str] = []

        try:
            loaded = json.loads(raw_value)
        except json.JSONDecodeError:
            normalised = raw_value.replace('\n', ',')
            for entry in normalised.split(','):
                normalised_url = self._normalise_url(entry)
                if normalised_url:
                    candidates.append(normalised_url)
        else:
            if isinstance(loaded, str):
                normalised_url = self._normalise_url(loaded)
                if normalised_url:
                    candidates.append(normalised_url)
            elif isinstance(loaded, (list, tuple)):
                for item in loaded:
                    if isinstance(item, str):
                        normalised_url = self._normalise_url(item)
                        if normalised_url:
                            candidates.append(normalised_url)
            else:
                logger.warning(
                    "Unsupported TOKEN_PLACE_RELAY_UPSTREAMS format: %s", type(loaded)
                )

        return candidates

    @staticmethod
    def _normalise_url(value: str) -> str:
        """Normalise URL-like strings for consistent comparisons."""

        if not value:
            return ''
        return value.strip().rstrip('/')

    @staticmethod
    def _parse_bool(value: Optional[str]) -> Optional[bool]:
        """Parse boolean-like environment overrides."""

        if value is None:
            return None

        lowered = str(value).strip().lower()
        if not lowered:
            return None

        if lowered in {'1', 'true', 'yes', 'on'}:
            return True
        if lowered in {'0', 'false', 'no', 'off'}:
            return False

        return None

    def _normalise_relay_server_pool(self) -> None:
        """Ensure relay.server_url and relay.server_pool stay consistent."""

        relay_config = self.config.setdefault('relay', {})
        server_url = self._normalise_url(relay_config.get('server_url', ''))
        pool = relay_config.get('server_pool') or []
        pool = [self._normalise_url(entry) for entry in pool if self._normalise_url(entry)]

        if server_url:
            if not pool or pool[0] != server_url:
                pool = [server_url] + [entry for entry in pool if entry != server_url]
        elif pool:
            server_url = pool[0]
        else:
            server_url = self._normalise_url(DEFAULT_CONFIG['relay']['server_url'])
            pool = [server_url]

        relay_config['server_url'] = server_url
        relay_config['server_pool'] = pool
        relay_config['server_pool_secondary'] = pool[1:]

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration value by dot-separated key path.
        E.g., config.get('server.port')
        """
        keys = key.split('.')
        value = self.config
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default

    def set(self, key: str, value: Any):
        """
        Set a configuration value by dot-separated key path.
        E.g., config.set('server.port', 8080)
        """
        keys = key.split('.')
        config = self.config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value

    def save_user_config(self, config_path: Optional[str] = None):
        """Save the current configuration to a user config file.

        When ``config_path`` points to a file inside a non-existent directory, the
        missing parent directories are created automatically before writing the
        file.
        """
        path = config_path or self.config_path
        if not path:
            path = os.path.join(self.config['paths']['config_dir'], 'user_config.json')

        # Import locally to avoid circular import at module load time
        from utils.path_handling import ensure_dir_exists

        ensure_dir_exists(os.path.dirname(path))

        try:
            with open(path, 'w') as f:
                json.dump(self.config, f, indent=2)
            logger.info(f"Configuration saved to {path}")
        except Exception as e:
            logger.error(f"Error saving configuration: {str(e)}")

    @property
    def is_windows(self) -> bool:
        """Check if the current platform is Windows"""
        return self.platform == 'windows'

    @property
    def is_macos(self) -> bool:
        """Check if the current platform is macOS"""
        return self.platform == 'darwin'

    @property
    def is_linux(self) -> bool:
        """Check if the current platform is Linux"""
        return self.platform == 'linux'

    @property
    def is_development(self) -> bool:
        """Check if the current environment is development"""
        return self.env == 'development'

    @property
    def is_testing(self) -> bool:
        """Check if the current environment is testing"""
        return self.env == 'testing'

    @property
    def is_production(self) -> bool:
        """Check if the current environment is production"""
        return self.env == 'production'

# Create a global config instance
config = Config()

def get_config() -> Config:
    """Get the global configuration instance"""
    return config
