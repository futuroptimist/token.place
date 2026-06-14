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
_IMMUTABLE_BRANCH_TAG_RE = re.compile(r"^[A-Za-z0-9._-]+-[0-9a-fA-F]{7,40}$")
_SEMVER_IMAGE_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
_FULL_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


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
                return _clean_public_token(raw_value.strip().strip("\"'"))
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


def resolve_release_version() -> str:
    """Resolve the public release version, falling back to local metadata then dev."""

    return (
        _clean_public_token(os.environ.get("TOKENPLACE_RELEASE_VERSION"))
        or _read_chart_app_version()
        or _read_package_version()
        or "dev"
    )


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


def resolve_image_tag() -> str:
    """Resolve an optional public image tag deployed by Helm or other orchestrators."""

    return _clean_public_token(os.environ.get("TOKENPLACE_IMAGE_TAG"), max_length=64)


def resolve_git_ref() -> str:
    """Resolve an optional public git ref as an immutable short deployment token."""

    git_sha = _clean_public_token(os.environ.get("TOKENPLACE_GIT_SHA"), max_length=64)
    if not git_sha:
        return ""
    if _FULL_GIT_SHA_RE.match(git_sha):
        return f"main-{git_sha[:7]}"
    return git_sha[:12]


def resolve_short_ref() -> str:
    """Resolve an optional public deploy ref, preferring image tags over git refs."""

    return resolve_image_tag() or resolve_git_ref()


def _is_immutable_image_tag(value: str) -> bool:
    return bool(
        value
        and (_IMMUTABLE_BRANCH_TAG_RE.match(value) or _SEMVER_IMAGE_TAG_RE.match(value))
    )


def _resolve_badge_version(
    environment: str, release_version: str, image_tag: str, git_ref: str
) -> tuple[str, str]:
    """Return ``(display_version, ref)`` for the public badge metadata."""

    deploy_ref = image_tag or git_ref
    if environment == "staging":
        if _is_immutable_image_tag(image_tag):
            return image_tag, image_tag
        if git_ref:
            return git_ref, git_ref
        if image_tag:
            return image_tag, image_tag
        return release_version, ""

    if environment == "prod":
        if release_version != "dev":
            return release_version, deploy_ref
        if _SEMVER_IMAGE_TAG_RE.match(image_tag):
            return image_tag, image_tag
        if deploy_ref:
            return deploy_ref, deploy_ref
        return release_version, ""

    if release_version != "dev":
        return release_version, deploy_ref
    if deploy_ref:
        return deploy_ref, deploy_ref
    return release_version, ""


def get_release_metadata(host: str | None = None) -> dict[str, str]:
    """Return public-safe release metadata for pages and JSON endpoints."""

    release_version = resolve_release_version()
    environment = infer_release_environment(host)
    image_tag = resolve_image_tag()
    git_ref = resolve_git_ref()
    display_version, deploy_ref = _resolve_badge_version(
        environment, release_version, image_tag, git_ref
    )
    metadata = {
        "environment": environment,
        "version": display_version,
        "label": f"{environment} {display_version}",
    }
    if (
        deploy_ref
        and deploy_ref != display_version
        or (environment == "staging" and deploy_ref)
    ):
        metadata["ref"] = deploy_ref
    return metadata


def release_metadata_json(host: str | None = None) -> str:
    """Return compact JSON suitable for embedding in static HTML."""

    return json.dumps(get_release_metadata(host), sort_keys=True, separators=(",", ":"))
