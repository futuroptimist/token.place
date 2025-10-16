"""Utilities for loading environment variable files in a predictable order."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from dotenv import dotenv_values

logger = logging.getLogger(__name__)


EXPLICIT_ENV_FILE_VAR = "TOKEN_PLACE_ENV_FILE"


@dataclass(frozen=True)
class EnvLoadResult:
    """Summary of the environment files loaded during bootstrap."""

    loaded_files: Tuple[Path, ...]
    applied_values: Mapping[str, str]
    resolved_env: Optional[str]
    explicit_file: Optional[Path]


def _default_root() -> Path:
    """Return the repository root assumed to hold the .env files."""

    return Path(__file__).resolve().parent.parent


def _normalise_explicit_path(explicit: Optional[os.PathLike[str] | str]) -> Optional[Path]:
    if not explicit:
        return None
    try:
        candidate = Path(explicit).expanduser().resolve()
    except OSError:
        logger.warning("Unable to resolve explicit env file path: %s", explicit)
        return None
    return candidate


def load_project_env(
    env: Optional[str] = None,
    *,
    root: Optional[Path] = None,
    explicit: Optional[os.PathLike[str] | str] = None,
) -> EnvLoadResult:
    """Load ``.env`` files following the documented precedence chain.

    The precedence is:

    1. ``.env``
    2. ``.env.<environment>`` (when the environment is known)
    3. ``.env.local``
    4. File referenced by ``TOKEN_PLACE_ENV_FILE`` (or ``explicit`` argument)

    Values already present in ``os.environ`` are never overridden. Later files in the
    chain override earlier ones when writing new keys into the environment.
    """

    search_root = root or _default_root()
    aggregated: Dict[str, str] = {}
    loaded_files: list[Path] = []

    def merge(path: Path) -> None:
        try:
            values = dotenv_values(path)
        except OSError as exc:
            logger.warning("Failed to read env file %s: %s", path, exc)
            return

        if not values:
            return

        for key, value in values.items():
            if value is not None:
                aggregated[key] = value

        loaded_files.append(path)

    base_file = search_root / ".env"
    if base_file.is_file():
        merge(base_file)

    resolved_env = env or aggregated.get("TOKEN_PLACE_ENV") or os.environ.get("TOKEN_PLACE_ENV")
    if resolved_env:
        env_file = search_root / f".env.{resolved_env}"
        if env_file.is_file():
            merge(env_file)

    local_file = search_root / ".env.local"
    if local_file.is_file():
        merge(local_file)

    explicit_path = _normalise_explicit_path(explicit or os.environ.get(EXPLICIT_ENV_FILE_VAR))
    if explicit_path:
        if explicit_path.is_file():
            merge(explicit_path)
        else:
            logger.warning("Explicit env file %s does not exist", explicit_path)

    applied: Dict[str, str] = {}
    for key, value in aggregated.items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value

    return EnvLoadResult(
        loaded_files=tuple(loaded_files),
        applied_values=applied,
        resolved_env=resolved_env,
        explicit_file=explicit_path,
    )


__all__ = ["EnvLoadResult", "load_project_env", "EXPLICIT_ENV_FILE_VAR"]
