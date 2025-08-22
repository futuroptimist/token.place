import os
import platform
import pathlib
import re
from typing import Optional, Union

# Define platform-specific constants
PLATFORM = platform.system().lower()
IS_WINDOWS = PLATFORM == "windows"
IS_MACOS = PLATFORM == "darwin"
IS_LINUX = PLATFORM == "linux"

_UNIX_ENV_PATTERN = re.compile(r"\$(?:\{[^}]+\}|[A-Za-z_][A-Za-z0-9_]*)")
_WIN_ENV_PATTERN = re.compile(r"%(?:[^%]+)%")


def _get_env(name: str) -> Optional[str]:
    """Return the value of ``name`` stripped of whitespace or ``None`` when unset."""
    value = os.environ.get(name)
    if value:
        value = value.strip()
        if value:
            return value
    return None


def _has_unexpanded_vars(path_str: str) -> bool:
    """Return True if ``path_str`` still contains environment variable markers."""
    return bool(_UNIX_ENV_PATTERN.search(path_str) or _WIN_ENV_PATTERN.search(path_str))

def get_user_home_dir() -> pathlib.Path:
    """Get the user's home directory path in a cross-platform way."""
    return pathlib.Path.home()

def get_app_data_dir() -> pathlib.Path:
    """
    Get the appropriate application data directory based on platform:
    - Windows: %APPDATA%/token.place
    - macOS: ~/Library/Application Support/token.place
    - Linux: $XDG_DATA_HOME/token.place or ~/.local/share/token.place
    """
    if IS_WINDOWS:
        appdata = _get_env('APPDATA')
        if appdata:
            base_dir = pathlib.Path(appdata)
        else:
            base_dir = get_user_home_dir() / 'AppData' / 'Roaming'
    elif IS_MACOS:
        base_dir = get_user_home_dir() / 'Library' / 'Application Support'
    else:  # Linux and other Unix-like
        xdg_data_home = _get_env('XDG_DATA_HOME')
        if xdg_data_home:
            base_dir = pathlib.Path(xdg_data_home)
        else:
            base_dir = get_user_home_dir() / '.local' / 'share'
    return ensure_dir_exists(base_dir / 'token.place')

def get_config_dir() -> pathlib.Path:
    """
    Get the appropriate configuration directory based on platform:
    - Windows: %APPDATA%/token.place/config
    - macOS: ~/Library/Application Support/token.place/config
    - Linux: $XDG_CONFIG_HOME/token.place/config or ~/.config/token.place/config
    """
    if IS_WINDOWS or IS_MACOS:
        return ensure_dir_exists(get_app_data_dir() / 'config')
    else:  # Linux and other Unix-like
        xdg_config_home = _get_env('XDG_CONFIG_HOME')
        if xdg_config_home:
            base_dir = pathlib.Path(xdg_config_home)
        else:
            base_dir = get_user_home_dir() / '.config'
        return ensure_dir_exists(base_dir / 'token.place' / 'config')

def get_cache_dir() -> pathlib.Path:
    """
    Get the appropriate cache directory based on platform:
    - Windows: %LOCALAPPDATA%/token.place/cache
    - macOS: ~/Library/Caches/token.place
    - Linux: $XDG_CACHE_HOME/token.place or ~/.cache/token.place
    """
    if IS_WINDOWS:
        local_appdata = _get_env('LOCALAPPDATA')
        if local_appdata:
            base_dir = pathlib.Path(local_appdata)
        else:
            base_dir = get_user_home_dir() / 'AppData' / 'Local'
        return ensure_dir_exists(base_dir / 'token.place' / 'cache')
    elif IS_MACOS:
        return ensure_dir_exists(get_user_home_dir() / 'Library' / 'Caches' / 'token.place')
    else:  # Linux and other Unix-like
        xdg_cache_home = _get_env('XDG_CACHE_HOME')
        if xdg_cache_home:
            base_dir = pathlib.Path(xdg_cache_home)
        else:
            base_dir = get_user_home_dir() / '.cache'
        return ensure_dir_exists(base_dir / 'token.place')

def get_models_dir() -> pathlib.Path:
    """Get the directory for storing downloaded models."""
    return ensure_dir_exists(get_app_data_dir() / 'models')

def get_logs_dir() -> pathlib.Path:
    """Get the directory for storing log files, creating it if missing.

    On Linux, honors the ``XDG_STATE_HOME`` environment variable when set.
    """
    if IS_WINDOWS:
        return ensure_dir_exists(get_app_data_dir() / 'logs')
    elif IS_MACOS:
        return ensure_dir_exists(
            get_user_home_dir() / 'Library' / 'Logs' / 'token.place'
        )
    else:  # Linux and other Unix-like
        xdg_state_home = _get_env('XDG_STATE_HOME')
        if xdg_state_home:
            base_dir = pathlib.Path(xdg_state_home)
        else:
            base_dir = get_user_home_dir() / '.local' / 'state'
        return ensure_dir_exists(base_dir / 'token.place' / 'logs')

def ensure_dir_exists(dir_path: Union[str, os.PathLike[str]]) -> pathlib.Path:
    """
    Ensure a directory exists, creating it if necessary.
    Accepts strings or ``os.PathLike`` objects, expands ``~`` and environment
    variables before creating the directory, and strips surrounding whitespace to
    avoid accidental directory names. Raises ``TypeError`` if ``dir_path`` is
    ``None``, ``ValueError`` when environment variables remain unexpanded or the
    path is empty, and ``NotADirectoryError`` if the path points to an existing
    file. Returns the path as a ``pathlib.Path`` object.
    """
    if dir_path is None:
        raise TypeError("dir_path cannot be None")
    if not isinstance(dir_path, (str, os.PathLike)):
        raise TypeError("dir_path must be path-like")

    # Expand environment variables and user home (~), then normalize
    # Also strip surrounding whitespace to avoid creating unintended paths
    path_str = os.path.expandvars(os.fspath(dir_path)).strip()
    if _has_unexpanded_vars(path_str):
        raise ValueError("dir_path contains unexpanded environment variables")
    if path_str == "":
        raise ValueError("dir_path cannot be empty")
    path = pathlib.Path(path_str).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"{path} exists and is not a directory")
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_executable_extension() -> str:
    """Get the appropriate executable extension for the current platform."""
    return '.exe' if IS_WINDOWS else ''

def normalize_path(path: Union[str, os.PathLike[str]]) -> pathlib.Path:
    """Convert a path to a normalized ``pathlib.Path`` object.

    Accepts strings or ``os.PathLike`` objects, strips surrounding whitespace,
    and expands environment variables and user home (``~``). Raises
    ``TypeError`` when ``path`` is ``None`` and ``ValueError`` when environment
    variables remain unexpanded or the path is empty.
    """
    if path is None:
        raise TypeError("path cannot be None")
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("path must be path-like")

    expanded = os.path.expandvars(os.fspath(path)).strip()
    if _has_unexpanded_vars(expanded):
        raise ValueError("path contains unexpanded environment variables")
    if expanded == "":
        raise ValueError("path cannot be empty")
    return pathlib.Path(expanded).expanduser().resolve()

def get_relative_path(
    path: Union[str, os.PathLike[str]],
    base_path: Optional[Union[str, os.PathLike[str]]] = None,
) -> pathlib.Path:
    """Return ``path`` relative to ``base_path``.

    If ``base_path`` is ``None`` the current working directory is used. When the
    two paths do not share a common ancestor, the returned path contains ``..``
    segments instead of an absolute path. On Windows, paths on different drives
    return the absolute ``path`` because ``os.path.relpath`` raises ``ValueError``
    in this scenario. Raises ``NotADirectoryError`` when ``base_path`` points to
    an existing file.
    """
    path = normalize_path(path)
    if base_path is None:
        base_path = pathlib.Path.cwd()
    else:
        base_path = normalize_path(base_path)
        if base_path.exists() and not base_path.is_dir():
            raise NotADirectoryError(f"{base_path} is not a directory")

    try:
        return path.relative_to(base_path)
    except ValueError:
        # If path is not relative to base_path, compute a relative path. On
        # Windows, ``os.path.relpath`` can raise ``ValueError`` when the paths
        # reside on different drives, in which case we return the absolute path
        # instead of bubbling up the exception.
        try:
            return pathlib.Path(os.path.relpath(path, base_path))
        except ValueError:
            return path
