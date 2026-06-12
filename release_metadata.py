"""Public-safe release metadata helpers for token.place deployments."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_RELEASE_VERSION = "dev"
DEFAULT_DEPLOY_ENV = "dev"
_PUBLIC_REF_RE = re.compile(r"[^A-Za-z0-9._:-]+")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _read_chart_app_version() -> str | None:
    chart_path = REPO_ROOT / "charts" / "tokenplace" / "Chart.yaml"
    try:
        for line in chart_path.read_text(encoding="utf-8").splitlines():
            if not line.strip().startswith("appVersion:"):
                continue
            _, value = line.split(":", 1)
            return _clean(value.strip().strip('"\''))
    except OSError:
        return None
    return None


def _read_desktop_package_version() -> str | None:
    package_path = REPO_ROOT / "desktop-tauri" / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return _clean(package.get("version"))


def release_version() -> str:
    """Resolve the public release version with environment overrides first."""

    return (
        _clean(os.environ.get("TOKENPLACE_RELEASE_VERSION"))
        or _read_chart_app_version()
        or _read_desktop_package_version()
        or DEFAULT_RELEASE_VERSION
    )


def _hostname_from_host(host: str | None) -> str | None:
    host_value = _clean(host)
    if not host_value:
        return None
    # Flask request.host may include a port; IPv6 localhost is not expected for
    # production inference but handle bracketed values safely for dev launches.
    if host_value.startswith("[") and "]" in host_value:
        return host_value[1:host_value.index("]")].lower()
    return host_value.split(":", 1)[0].lower()


def infer_deploy_env_from_host(host: str | None) -> str | None:
    """Infer a public deployment environment from a request host."""

    hostname = _hostname_from_host(host)
    if hostname == "token.place":
        return "prod"
    if hostname == "staging.token.place":
        return "staging"
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return "dev"
    return None


def deploy_environment(host: str | None = None) -> str:
    """Resolve the public deployment environment with host inference fallback."""

    return (
        _clean(os.environ.get("TOKENPLACE_DEPLOY_ENV"))
        or _clean(os.environ.get("TOKEN_PLACE_ENV"))
        or infer_deploy_env_from_host(host)
        or DEFAULT_DEPLOY_ENV
    )


def short_release_ref() -> str | None:
    """Return an optional public-safe short git SHA or image tag."""

    git_sha = _clean(os.environ.get("TOKENPLACE_GIT_SHA"))
    if git_sha:
        return _PUBLIC_REF_RE.sub("-", git_sha)[:12]

    image_tag = _clean(os.environ.get("TOKENPLACE_IMAGE_TAG"))
    if image_tag:
        return _PUBLIC_REF_RE.sub("-", image_tag)[:40]

    return None


def release_metadata(host: str | None = None) -> dict[str, Any]:
    """Build the public-safe release metadata payload for web/UI consumers."""

    version = release_version()
    environment = deploy_environment(host)
    ref = short_release_ref()
    display_version = ref if environment == "staging" and ref else version
    metadata: dict[str, Any] = {
        "environment": environment,
        "version": version,
        "display_version": display_version,
        "badge_label": f"{environment} {display_version}",
    }
    if ref:
        metadata["ref"] = ref
    return metadata


def release_metadata_json(host: str | None = None) -> str:
    """Serialize public release metadata for safe inline HTML injection."""

    return json.dumps(release_metadata(host), sort_keys=True, separators=(",", ":"))
