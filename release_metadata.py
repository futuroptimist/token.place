"""Public-safe token.place release metadata helpers."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent
_PUBLIC_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SEMVER_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
_IMMUTABLE_IMAGE_TAG_RE = re.compile(
    r"^(?:main|master|staging|prod|release)-[0-9a-fA-F]{7,40}$|^v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$"
)


def _clean_public_token(value: Any, *, max_length: int = 64) -> str:
    """Return a conservative public metadata token with unsafe characters removed."""

    text = str(value or "").strip()
    if not text:
        return ""
    text = _PUBLIC_TOKEN_RE.sub("-", text).strip(".-_")
    return text[:max_length]


@lru_cache(maxsize=1)
def _read_chart_app_version() -> str:
    chart_path = _REPO_ROOT / "charts" / "tokenplace" / "Chart.yaml"
    try:
        for line in chart_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("appVersion:"):
                _, raw_value = line.split(":", 1)
                return _clean_public_token(raw_value.strip().strip('"\''))
    except OSError:
        return ""
    return ""


@lru_cache(maxsize=1)
def _read_package_version() -> str:
    package_path = _REPO_ROOT / "desktop-tauri" / "package.json"
    try:
        data = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return _clean_public_token(data.get("version"))


def _release_version_from_env() -> str:
    return _clean_public_token(os.environ.get("TOKENPLACE_RELEASE_VERSION"))


def resolve_release_version() -> str:
    """Resolve the public app release version, falling back to local metadata then dev."""

    return _release_version_from_env() or _read_chart_app_version() or _read_package_version() or "dev"


def _hostname_from_host(host: str | None) -> str:
    if not host:
        return ""
    candidate = host.strip().lower()
    if "://" in candidate:
        candidate = candidate.split("://", 1)[1]
    candidate = candidate.split("/", 1)[0]
    if candidate.startswith("[") and "]" in candidate:
        return candidate[1 : candidate.index("]")]
    return candidate.split(":", 1)[0]


def infer_release_environment(host: str | None = None) -> str:
    """Resolve the public deploy environment from explicit deploy env or request host."""

    explicit = os.environ.get("TOKENPLACE_DEPLOY_ENV")
    if explicit:
        normalized = explicit.strip().lower()
        aliases = {
            "production": "prod",
            "prod": "prod",
            "staging": "staging",
            "stage": "staging",
            "development": "dev",
            "local": "dev",
            "localhost": "dev",
            "testing": "dev",
            "test": "dev",
            "dev": "dev",
        }
        return aliases.get(normalized, "dev")

    hostname = _hostname_from_host(host)
    if hostname == "token.place":
        return "prod"
    if hostname == "staging.token.place":
        return "staging"
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "dev"
    return "dev"


def _image_tag() -> str:
    return _clean_public_token(os.environ.get("TOKENPLACE_IMAGE_TAG"), max_length=48)


def _short_git_sha() -> str:
    git_sha = _clean_public_token(os.environ.get("TOKENPLACE_GIT_SHA"), max_length=64)
    return git_sha[:12] if git_sha else ""


def _is_semver_tag(value: str) -> bool:
    return bool(_SEMVER_TAG_RE.match(value))


def _is_immutable_image_tag(value: str) -> bool:
    return bool(_IMMUTABLE_IMAGE_TAG_RE.match(value))


def resolve_deploy_ref() -> str:
    """Resolve an optional public deploy ref from image tag or short git SHA."""

    return _image_tag() or _short_git_sha()


def _immutable_git_ref() -> str:
    git_sha = _short_git_sha()
    if not git_sha:
        return ""
    return git_sha if "-" in git_sha else f"main-{git_sha}"


def resolve_short_ref() -> str:
    """Resolve an optional public deploy ref kept for backward compatibility."""

    return resolve_deploy_ref()


def _staging_display_version(release_version: str, image_tag: str, deploy_ref: str) -> str:
    if image_tag and _is_immutable_image_tag(image_tag):
        return image_tag
    git_ref = _immutable_git_ref()
    if git_ref:
        return git_ref
    if release_version != "dev":
        return release_version
    return deploy_ref or "dev"


def _prod_display_version(release_version: str, image_tag: str, deploy_ref: str) -> str:
    if _release_version_from_env():
        return release_version
    if image_tag and _is_semver_tag(image_tag):
        return image_tag
    if release_version != "dev":
        return release_version
    return deploy_ref or "dev"


def get_release_metadata(host: str | None = None) -> dict[str, str]:
    """Return public-safe release metadata for pages and JSON endpoints."""

    release_version = resolve_release_version()
    environment = infer_release_environment(host)
    image_tag = _image_tag()
    deploy_ref = resolve_deploy_ref()

    if environment == "staging":
        display_version = _staging_display_version(release_version, image_tag, deploy_ref)
    elif environment == "prod":
        display_version = _prod_display_version(release_version, image_tag, deploy_ref)
    else:
        display_version = release_version if release_version != "dev" else (deploy_ref or "dev")

    metadata = {
        "environment": environment,
        "version": display_version,
        "label": f"{environment} {display_version}",
    }
    if deploy_ref and (deploy_ref != display_version or environment == "staging"):
        metadata["ref"] = deploy_ref
    return metadata


def release_metadata_json(host: str | None = None) -> str:
    """Return compact JSON suitable for embedding in static HTML."""

    return json.dumps(get_release_metadata(host), sort_keys=True, separators=(",", ":"))
