import os
import sys
import platform
import pathlib
from typing import Optional, Union, List

# Define platform-specific constants
PLATFORM = platform.system().lower()
IS_WINDOWS = PLATFORM == "windows"
IS_MACOS = PLATFORM == "darwin"
IS_LINUX = PLATFORM == "linux"

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
        appdata = os.environ.get('APPDATA')
        if appdata:
            base_dir = pathlib.Path(appdata)
        else:
            base_dir = get_user_home_dir() / 'AppData' / 'Roaming'
    elif IS_MACOS:
        base_dir = get_user_home_dir() / 'Library' / 'Application Support'
    else:  # Linux and other Unix-like
        xdg_data_home = os.environ.get('XDG_DATA_HOME')
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
    - Linux: $XDG_CONFIG_HOME/token.place or ~/.config/token.place
    """
    if IS_WINDOWS or IS_MACOS:
        return ensure_dir_exists(get_app_data_dir() / 'config')
    else:  # Linux and other Unix-like
        xdg_config_home = os.environ.get('XDG_CONFIG_HOME')
        if xdg_config_home:
            base_dir = pathlib.Path(xdg_config_home)
        else:
            base_dir = get_user_home_dir() / '.config'
        return ensure_dir_exists(base_dir / 'token.place')

def get_cache_dir() -> pathlib.Path:
    """
    Get the appropriate cache directory based on platform:
    - Windows: %LOCALAPPDATA%/token.place/cache
    - macOS: ~/Library/Caches/token.place
    - Linux: $XDG_CACHE_HOME/token.place or ~/.cache/token.place
    """
    if IS_WINDOWS:
        local_appdata = os.environ.get('LOCALAPPDATA')
        if local_appdata:
            base_dir = pathlib.Path(local_appdata)
        else:
            base_dir = get_user_home_dir() / 'AppData' / 'Local'
        return ensure_dir_exists(base_dir / 'token.place' / 'cache')
    elif IS_MACOS:
        return ensure_dir_exists(get_user_home_dir() / 'Library' / 'Caches' / 'token.place')
    else:  # Linux and other Unix-like
        xdg_cache_home = os.environ.get('XDG_CACHE_HOME')
        if xdg_cache_home:
            base_dir = pathlib.Path(xdg_cache_home)
        else:
            base_dir = get_user_home_dir() / '.cache'
        return ensure_dir_exists(base_dir / 'token.place')

def get_models_dir() -> pathlib.Path:
    """Get the directory for storing downloaded models."""
    return ensure_dir_exists(get_app_data_dir() / 'models')

def get_logs_dir() -> pathlib.Path:
    """Get the directory for storing log files.

    On Linux, honors the ``XDG_STATE_HOME`` environment variable when set.
    """
    if IS_WINDOWS:
        return ensure_dir_exists(get_app_data_dir() / 'logs')
    elif IS_MACOS:
        return ensure_dir_exists(
            get_user_home_dir() / 'Library' / 'Logs' / 'token.place'
        )
    else:  # Linux and other Unix-like
        xdg_state_home = os.environ.get('XDG_STATE_HOME')
        if xdg_state_home:
            base_dir = pathlib.Path(xdg_state_home)
        else:
            base_dir = get_user_home_dir() / '.local' / 'state'
        return ensure_dir_exists(base_dir / 'token.place' / 'logs')

def ensure_dir_exists(dir_path: Union[str, pathlib.Path]) -> pathlib.Path:
    """Ensure a directory exists, creating it if necessary.

    Expands ``~`` and environment variables before creating the directory.
    Raises ``NotADirectoryError`` if the path points to an existing file.
    The returned path is absolute with symlinks preserved.
    """
    # Expand environment variables and user home (~), then normalize
    path_str = os.path.expandvars(str(dir_path))
    path = pathlib.Path(path_str).expanduser().absolute()
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"{path} exists and is not a directory")
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_executable_extension() -> str:
    """Get the appropriate executable extension for the current platform."""
    return '.exe' if IS_WINDOWS else ''

def normalize_path(path: Union[str, pathlib.Path]) -> pathlib.Path:
    """Convert a path string to a normalized pathlib.Path object."""
    expanded = os.path.expandvars(str(path))
    return pathlib.Path(expanded).expanduser().resolve()

def get_relative_path(path: Union[str, pathlib.Path], base_path: Optional[Union[str, pathlib.Path]] = None) -> pathlib.Path:
    """Return ``path`` relative to ``base_path``.

    If ``base_path`` is ``None`` the current working directory is used. When the
    two paths do not share a common ancestor, the returned path contains ``..``
    segments instead of an absolute path. On Windows, paths on different drives
    return the absolute ``path`` because ``os.path.relpath`` raises ``ValueError``
    in this scenario.
    """
    path = normalize_path(path)
    if base_path is None:
        base_path = pathlib.Path.cwd()
    else:
        base_path = normalize_path(base_path)

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
