"""Public-safe release metadata helpers for relay-rendered pages."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping

_REPO_ROOT = Path(__file__).resolve().parent
_CHART_PATH = _REPO_ROOT / "charts" / "tokenplace" / "Chart.yaml"
_DESKTOP_PACKAGE_PATH = _REPO_ROOT / "desktop-tauri" / "package.json"
_SAFE_VALUE_RE = re.compile(r"[^A-Za-z0-9._+-]+")
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _clean_public_value(value: object, *, max_length: int = 128) -> str:
    """Return a small public-safe token suitable for HTML/JSON metadata."""

    if not isinstance(value, str):
        return ""
    cleaned = _SAFE_VALUE_RE.sub("-", value.strip())[:max_length].strip("-._+")
    return cleaned


def _env_value(environ: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = _clean_public_value(environ.get(name, ""))
        if value:
            return value
    return ""


def _chart_app_version() -> str:
    try:
        text = _CHART_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""

    for line in text.splitlines():
        if not line.strip().startswith("appVersion:"):
            continue
        _, raw_value = line.split(":", 1)
        return _clean_public_value(raw_value.strip().strip("'\""))
    return ""


def _desktop_package_version() -> str:
    try:
        data = json.loads(_DESKTOP_PACKAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return _clean_public_value(data.get("version", ""))


def resolve_release_version(environ: Mapping[str, str] | None = None) -> str:
    """Resolve the public release version, falling back to development metadata."""

    env = environ or os.environ
    return (
        _env_value(env, "TOKENPLACE_RELEASE_VERSION", "TOKENPLACE_CHART_APP_VERSION")
        or _chart_app_version()
        or _desktop_package_version()
        or "dev"
    )


def _host_without_port(host: str | None) -> str:
    if not host:
        return ""
    candidate = host.strip().lower()
    if candidate.startswith("[") and "]" in candidate:
        return candidate[1 : candidate.index("]")]
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]
    return candidate.strip(".")


def resolve_release_environment(
    host: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """Resolve the public deploy environment from env, then request host."""

    env = environ or os.environ
    env_value = _env_value(env, "TOKENPLACE_DEPLOY_ENV", "TOKEN_PLACE_ENV")
    normalized_env = env_value.lower()
    if normalized_env:
        if normalized_env in {"production", "prod"}:
            return "prod"
        if normalized_env in {"stage", "staging"}:
            return "staging"
        if normalized_env in {"development", "dev", "local", "localhost"}:
            return "dev"
        return normalized_env

    normalized_host = _host_without_port(host)
    if normalized_host == "token.place":
        return "prod"
    if normalized_host == "staging.token.place":
        return "staging"
    if normalized_host in {"localhost", "127.0.0.1", "::1"}:
        return "dev"
    return "dev"


def resolve_release_ref(environ: Mapping[str, str] | None = None) -> str:
    """Return an optional short git SHA or image tag safe for public display."""

    env = environ or os.environ
    git_sha = _clean_public_value(env.get("TOKENPLACE_GIT_SHA", ""), max_length=40)
    if _GIT_SHA_RE.fullmatch(git_sha):
        return git_sha[:12]

    image_tag = _clean_public_value(env.get("TOKENPLACE_IMAGE_TAG", ""), max_length=128)
    if image_tag and _IMAGE_TAG_RE.fullmatch(image_tag):
        return image_tag
    return ""


def _display_version(version: str) -> str:
    semantic_version_pattern = r"\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9._-]+)?"
    if (
        version == "dev"
        or version.startswith("v")
        or not re.fullmatch(semantic_version_pattern, version)
    ):
        return version
    return f"v{version}"


def build_release_metadata(
    host: str | None = None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build public-safe release metadata for the landing page and API."""

    env = environ or os.environ
    version = resolve_release_version(env)
    deploy_env = resolve_release_environment(host, env)
    ref = resolve_release_ref(env)
    display_value = _display_version(version) if version != "dev" else (ref or "dev")
    label = f"{deploy_env} {display_value}".strip()
    metadata = {
        "environment": deploy_env,
        "version": version,
        "display_version": display_value,
        "label": label,
    }
    if ref:
        metadata["ref"] = ref
    return metadata
