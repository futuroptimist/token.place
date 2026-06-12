"""Public-safe token.place release metadata helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent
_PUBLIC_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _clean_public_token(value: Any, *, max_length: int = 64) -> str:
    """Return a conservative public metadata token with unsafe characters removed."""

    text = str(value or "").strip()
    if not text:
        return ""
    text = _PUBLIC_TOKEN_RE.sub("-", text).strip(".-_")
    return text[:max_length]


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
    """Resolve the public deploy environment from env vars or request host."""

    explicit = os.environ.get("TOKENPLACE_DEPLOY_ENV") or os.environ.get("TOKEN_PLACE_ENV")
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
            "dev": "dev",
        }
        return aliases.get(normalized, _clean_public_token(normalized, max_length=32) or "dev")

    hostname = _hostname_from_host(host)
    if hostname == "token.place":
        return "prod"
    if hostname == "staging.token.place":
        return "staging"
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "dev"
    return "dev"


def resolve_short_ref() -> str:
    """Resolve an optional public short git SHA or image tag."""

    git_sha = _clean_public_token(os.environ.get("TOKENPLACE_GIT_SHA"), max_length=64)
    if git_sha:
        return git_sha[:12]
    return _clean_public_token(os.environ.get("TOKENPLACE_IMAGE_TAG"), max_length=48)


def get_release_metadata(host: str | None = None) -> dict[str, str]:
    """Return public-safe release metadata for pages and JSON endpoints."""

    version = resolve_release_version()
    environment = infer_release_environment(host)
    short_ref = resolve_short_ref()
    badge_value = version if version != "dev" else (short_ref or version)
    metadata = {
        "environment": environment,
        "version": version,
        "label": f"{environment} {badge_value}",
    }
    if short_ref:
        metadata["ref"] = short_ref
    return metadata


def release_metadata_json(host: str | None = None) -> str:
    """Return compact JSON suitable for embedding in static HTML."""

    return json.dumps(get_release_metadata(host), sort_keys=True, separators=(",", ":"))
