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
    - Linux: ~/.local/share/token.place
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
        base_dir = get_user_home_dir() / '.local' / 'share'

    return base_dir / 'token.place'

def get_config_dir() -> pathlib.Path:
    """
    Get the appropriate configuration directory based on platform:
    - Windows: %APPDATA%/token.place/config
    - macOS: ~/Library/Application Support/token.place/config
    - Linux: ~/.config/token.place
    """
    if IS_WINDOWS or IS_MACOS:
        return get_app_data_dir() / 'config'
    else:  # Linux and other Unix-like
        return get_user_home_dir() / '.config' / 'token.place'

def get_cache_dir() -> pathlib.Path:
    """
    Get the appropriate cache directory based on platform:
    - Windows: %LOCALAPPDATA%/token.place/cache
    - macOS: ~/Library/Caches/token.place
    - Linux: ~/.cache/token.place
    """
    if IS_WINDOWS:
        local_appdata = os.environ.get('LOCALAPPDATA')
        if local_appdata:
            base_dir = pathlib.Path(local_appdata)
        else:
            base_dir = get_user_home_dir() / 'AppData' / 'Local'
        return base_dir / 'token.place' / 'cache'
    elif IS_MACOS:
        return get_user_home_dir() / 'Library' / 'Caches' / 'token.place'
    else:  # Linux and other Unix-like
        return get_user_home_dir() / '.cache' / 'token.place'

def get_models_dir() -> pathlib.Path:
    """Get the directory for storing downloaded models."""
    return get_app_data_dir() / 'models'

def get_logs_dir() -> pathlib.Path:
    """Get the directory for storing log files."""
    if IS_WINDOWS:
        return get_app_data_dir() / 'logs'
    elif IS_MACOS:
        return get_user_home_dir() / 'Library' / 'Logs' / 'token.place'
    else:  # Linux and other Unix-like
        return get_user_home_dir() / '.local' / 'state' / 'token.place' / 'logs'

def ensure_dir_exists(dir_path: Union[str, pathlib.Path]) -> pathlib.Path:
    """
    Ensure a directory exists, creating it if necessary.
    Raises NotADirectoryError if the path points to an existing file.
    Returns the path as a pathlib.Path object.
    """
    # Expand user home (~) and normalize to an absolute path
    path = pathlib.Path(dir_path).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(f"{path} exists and is not a directory")
    path.mkdir(parents=True, exist_ok=True)
    return path

def get_executable_extension() -> str:
    """Get the appropriate executable extension for the current platform."""
    return '.exe' if IS_WINDOWS else ''

def normalize_path(path: Union[str, pathlib.Path]) -> pathlib.Path:
    """Convert a path string to a normalized pathlib.Path object."""
    return pathlib.Path(path).expanduser().resolve()

def get_relative_path(path: Union[str, pathlib.Path], base_path: Optional[Union[str, pathlib.Path]] = None) -> pathlib.Path:
    """
    Get a path relative to the base path.
    If base_path is None, uses the current working directory.
    """
    path = normalize_path(path)
    if base_path is None:
        base_path = pathlib.Path.cwd()
    else:
        base_path = normalize_path(base_path)

    try:
        return path.relative_to(base_path)
    except ValueError:
        # If path is not relative to base_path, return the absolute path
        return path
