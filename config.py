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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, cast

from utils.config_schema import (
    APISettings,
    AppConfig,
    ConstantsSettings,
    DEFAULT_CONFIG,
    ENV_OVERRIDES,
    ModelSettings,
    PathsSettings,
    RelaySettings,
    SecuritySettings,
    ServerSettings,
)
from utils.env_loader import EnvLoadResult, load_project_env

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


@dataclass(frozen=True)
class SensitiveKey:
    """Represents a dot-delimited config key that should never persist to disk."""

    path: str

    @property
    def parts(self) -> List[str]:
        """Return the split path segments for traversal."""

        return self.path.split('.')


SENSITIVE_CONFIG_KEYS: List[SensitiveKey] = [
    SensitiveKey("relay.server_registration_token"),
]

class Config:
    """Configuration manager for token.place"""

    def __init__(self, env: Optional[str] = None, config_path: Optional[str] = None):
        """
        Initialize configuration with the specified environment.
        If env is None, it tries to read from TOKEN_PLACE_ENV environment variable,
        defaulting to 'development' if not set.
        """
        # Load environment variable files before inspecting the environment name.
        self.env_bootstrap: EnvLoadResult = load_project_env(env)
        self.loaded_env_files = self.env_bootstrap.loaded_files
        self.applied_env_values = dict(self.env_bootstrap.applied_values)

        # Determine the environment
        resolved_env = (
            env
            or self.env_bootstrap.resolved_env
            or os.environ.get('TOKEN_PLACE_ENV')
            or 'development'
        )
        self.env = resolved_env
        os.environ.setdefault('TOKEN_PLACE_ENV', self.env)

        if self.loaded_env_files:
            logger.debug(
                "Loaded environment files for %s: %s",
                self.env,
                ", ".join(str(path) for path in self.loaded_env_files),
            )

        # Detect platform if not already set
        self.platform = os.environ.get('PLATFORM', platform.system().lower())

        # Load base configuration using a deep copy to avoid mutating DEFAULT_CONFIG
        self.config: AppConfig = copy.deepcopy(DEFAULT_CONFIG)

        # Apply environment-specific overrides
        if self.env in ENV_OVERRIDES:
            # Copy to avoid mutating the shared override dictionaries when we patch
            # production values with environment variables in _apply_runtime_env_overrides.
            overrides = copy.deepcopy(ENV_OVERRIDES[self.env])
            if self.env == "production":
                self._apply_production_defaults(overrides)
            self._merge_configs(self.config, overrides)

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

        cf_env = self._gather_cloudflare_fallbacks()
        if cf_env is not None:
            self.set('relay.cloudflare_fallback_urls', cf_env)

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

    def _apply_production_defaults(self, overrides: Dict[str, Any]) -> None:
        """Populate production overrides with environment aware defaults."""

        server_overrides = overrides.setdefault('server', {})
        server_overrides.setdefault('host', os.environ.get('PROD_SERVER_HOST', '127.0.0.1'))

        relay_overrides = overrides.setdefault('relay', {})
        relay_overrides.setdefault('host', os.environ.get('PROD_RELAY_HOST', '127.0.0.1'))

        api_overrides = overrides.setdefault('api', {})
        api_overrides.setdefault('host', os.environ.get('PROD_API_HOST', '127.0.0.1'))

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

    def _gather_cloudflare_fallbacks(self) -> Optional[List[str]]:
        """Collect Cloudflare fallback URLs from config overrides."""

        env_value = os.environ.get('TOKEN_PLACE_RELAY_CLOUDFLARE_URLS', '').strip()
        single_env = os.environ.get('TOKEN_PLACE_RELAY_CLOUDFLARE_URL', '').strip()

        candidates: List[str] = []
        configured = self.get('relay.cloudflare_fallback_urls', []) or []

        for entry in configured:
            normalised = self._normalise_url(entry)
            if normalised:
                candidates.append(normalised)

        if env_value:
            candidates.extend(self._parse_relay_upstreams(env_value))

        if single_env:
            normalised_single = self._normalise_url(single_env)
            if normalised_single:
                candidates.append(normalised_single)

        deduped: List[str] = []
        seen = set()
        for url in candidates:
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

    def _redacted_config_copy(self) -> Dict[str, Any]:
        """Return a deepcopy of the config with sensitive values removed."""

        redacted = copy.deepcopy(self.config)
        for key in SENSITIVE_CONFIG_KEYS:
            cursor: Any = redacted
            parts = key.parts
            for segment in parts[:-1]:
                if not isinstance(cursor, dict):
                    cursor = None
                    break
                cursor = cursor.get(segment)
                if cursor is None:
                    break
            else:
                if isinstance(cursor, dict) and parts[-1] in cursor:
                    cursor[parts[-1]] = None
            # When the loop breaks early we continue to next key automatically.
        return redacted

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
            redacted = self._redacted_config_copy()
            with open(path, 'w') as f:
                json.dump(redacted, f, indent=2)
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

    # ------------------------------------------------------------------
    # Typed section helpers
    # ------------------------------------------------------------------

    @property
    def server_settings(self) -> ServerSettings:
        return cast(ServerSettings, self.config['server'])

    @property
    def relay_settings(self) -> RelaySettings:
        return cast(RelaySettings, self.config['relay'])

    @property
    def api_settings(self) -> APISettings:
        return cast(APISettings, self.config['api'])

    @property
    def security_settings(self) -> SecuritySettings:
        return cast(SecuritySettings, self.config['security'])

    @property
    def paths_settings(self) -> PathsSettings:
        return cast(PathsSettings, self.config['paths'])

    @property
    def model_settings(self) -> ModelSettings:
        return cast(ModelSettings, self.config['model'])

    @property
    def constants(self) -> ConstantsSettings:
        return cast(ConstantsSettings, self.config['constants'])

# Create a global config instance
config = Config()

def get_config() -> Config:
    """Get the global configuration instance"""
    return config
