"""Validate Linux no-bundle desktop staging layout contains Python runtime resources."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LINUX_STAGING_ROOT = REPO_ROOT / "desktop-tauri" / "src-tauri" / "target" / "debug"


def _staging_root() -> Path:
    override = os.environ.get("TOKEN_PLACE_DESKTOP_STAGING_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_LINUX_STAGING_ROOT


def _assert_any_exists(staging_root: Path, candidates: tuple[str, ...], label: str) -> None:
    resolved = [staging_root / candidate for candidate in candidates]
    if not any(path.exists() for path in resolved):
        joined = ", ".join(str(path) for path in resolved)
        raise AssertionError(f"missing packaged runtime {label}; looked for: {joined}")


def test_linux_no_bundle_layout_contains_python_runtime_resources() -> None:
    """CI proxy check: verify `tauri build -- --debug --no-bundle` staged runtime files."""

    staging_root = _staging_root()
    if not staging_root.exists():
        pytest.skip(
            "desktop staging root missing; run `npm --prefix desktop-tauri run tauri build -- --debug --no-bundle` "
            "before this validation"
        )
    if not (staging_root / "python").exists() and not (staging_root / "resources" / "python").exists():
        pytest.skip(
            "desktop staging root exists but no packaged python runtime tree was found; "
            "run `npm --prefix desktop-tauri run tauri build -- --debug --no-bundle` before this validation"
        )

    # Linux CI no-bundle builds stage files under `desktop-tauri/src-tauri/target/debug`.
    # We accept both known script placements because Tauri layout can differ by host packaging mode.
    _assert_any_exists(
        staging_root,
        ("python/model_bridge.py", "resources/python/model_bridge.py"),
        "python/model_bridge.py",
    )
    _assert_any_exists(
        staging_root,
        ("python/compute_node_bridge.py", "resources/python/compute_node_bridge.py"),
        "python/compute_node_bridge.py",
    )
    _assert_any_exists(
        staging_root,
        ("python/path_bootstrap.py", "resources/python/path_bootstrap.py"),
        "python/path_bootstrap.py",
    )
    _assert_any_exists(staging_root, ("utils", "resources/utils"), "utils/")
    _assert_any_exists(staging_root, ("config.py", "resources/config.py"), "config.py")
    _assert_any_exists(
        staging_root,
        ("requirements.txt", "resources/requirements.txt"),
        "requirements.txt",
    )


if __name__ == "__main__":
    test_linux_no_bundle_layout_contains_python_runtime_resources()
