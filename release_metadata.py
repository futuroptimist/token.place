"""Public-safe release metadata helpers for relay-served surfaces."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

_REPO_ROOT = Path(__file__).resolve().parent
_CHART_PATH = _REPO_ROOT / "charts" / "tokenplace" / "Chart.yaml"
_DESKTOP_PACKAGE_PATH = _REPO_ROOT / "desktop-tauri" / "package.json"
_SAFE_VALUE_RE = re.compile(r"[^A-Za-z0-9._:+\-/]")


def _clean_public_value(value: object, *, max_length: int = 80) -> str:
    """Return a bounded, public-display-safe metadata value."""

    text = str(value or "").strip()
    if not text:
        return ""
    return _SAFE_VALUE_RE.sub("-", text)[:max_length]


def _read_chart_app_version() -> str:
    """Read chart appVersion without importing a YAML dependency."""

    try:
        for raw_line in _CHART_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line.startswith("appVersion:"):
                continue
            _, value = line.split(":", 1)
            return _clean_public_value(value.strip().strip('"\''))
    except OSError:
        return ""
    return ""


def _read_desktop_package_version() -> str:
    try:
        payload = json.loads(_DESKTOP_PACKAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return _clean_public_value(payload.get("version"))


def resolve_release_version(env: Mapping[str, str] | None = None) -> str:
    """Resolve the public release version for the current process."""

    environ = env or os.environ
    return (
        _clean_public_value(environ.get("TOKENPLACE_RELEASE_VERSION"))
        or _read_chart_app_version()
        or _read_desktop_package_version()
        or "dev"
    )


def _normalize_host(host: str | None) -> str:
    host_text = str(host or "").strip().lower()
    if not host_text:
        return ""
    # X-Forwarded-Host can be a comma-separated chain. Use the original host.
    host_text = host_text.split(",", 1)[0].strip()
    if host_text.startswith("[") and "]" in host_text:
        return host_text[1 : host_text.index("]")]
    return host_text.split(":", 1)[0]


def infer_release_environment(host: str | None = None, env: Mapping[str, str] | None = None) -> str:
    """Resolve prod/staging/dev public environment from env vars or request host."""

    environ = env or os.environ
    deploy_env = _clean_public_value(environ.get("TOKENPLACE_DEPLOY_ENV"), max_length=32)
    if deploy_env:
        return deploy_env

    token_place_env = _clean_public_value(environ.get("TOKEN_PLACE_ENV"), max_length=32)
    if token_place_env and token_place_env not in {"dev", "development", "test", "testing"}:
        return token_place_env

    normalized_host = _normalize_host(host)
    if normalized_host == "token.place":
        return "prod"
    if normalized_host == "staging.token.place":
        return "staging"
    if normalized_host in {"localhost", "127.0.0.1", "::1"}:
        return "dev"
    return "dev"


def resolve_release_ref(env: Mapping[str, str] | None = None) -> str:
    """Resolve a short, optional public git/image reference."""

    environ = env or os.environ
    git_sha = _clean_public_value(environ.get("TOKENPLACE_GIT_SHA"), max_length=40)
    if git_sha:
        return git_sha[:12]
    return _clean_public_value(environ.get("TOKENPLACE_IMAGE_TAG"), max_length=40)


def get_release_metadata(host: str | None = None, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return public-safe release metadata for HTML and JSON responses."""

    environ = env or os.environ
    version = resolve_release_version(environ)
    deploy_env = infer_release_environment(host, environ)
    git_ref = resolve_release_ref(environ)
    display_version = git_ref if git_ref and git_ref != version else version
    return {
        "version": version,
        "environment": deploy_env,
        "ref": git_ref,
        "display_version": display_version,
        "badge": f"{deploy_env} {display_version}",
    }
