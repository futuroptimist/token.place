"""Utilities for loading the token.place server provider registry."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

import yaml


class ProviderRegistryError(RuntimeError):
    """Raised when the provider registry cannot be loaded."""


def _registry_path() -> Path:
    """Return the configured path to the provider registry file."""

    override = os.getenv("TOKEN_PLACE_PROVIDER_REGISTRY")
    if override:
        return Path(override)

    return Path(__file__).resolve().parents[2] / "config" / "server_providers.yaml"


@lru_cache(maxsize=1)
def get_provider_directory() -> Dict[str, Any]:
    """Load and normalise the provider directory from disk."""

    path = _registry_path()
    if not path.exists():
        raise ProviderRegistryError(
            f"Provider registry file not found at {path}."
        )

    try:
        with path.open("r", encoding="utf-8") as handle:
            raw_data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ProviderRegistryError("Failed to parse provider registry YAML") from exc

    if not isinstance(raw_data, dict):
        raise ProviderRegistryError("Provider registry root must be a mapping")

    metadata = raw_data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ProviderRegistryError("Provider registry metadata must be a mapping")

    providers = raw_data.get("providers") or []
    if not isinstance(providers, list):
        raise ProviderRegistryError("Provider registry providers must be a list")

    normalised: List[Dict[str, Any]] = []
    for entry in providers:
        if not isinstance(entry, dict):
            raise ProviderRegistryError("Each provider entry must be a mapping")

        missing_fields = [
            field for field in ("id", "name", "region", "status") if field not in entry
        ]
        if missing_fields:
            raise ProviderRegistryError(
                "Provider entry missing required fields: " + ", ".join(missing_fields)
            )

        endpoints = entry.get("endpoints") or []
        if not isinstance(endpoints, list):
            raise ProviderRegistryError(
                f"Provider '{entry['id']}' endpoints must be a list"
            )

        normalised_endpoints: List[Dict[str, Any]] = []
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                raise ProviderRegistryError(
                    f"Provider '{entry['id']}' has an invalid endpoint definition"
                )

            missing_endpoint_fields = [
                field for field in ("type", "url") if field not in endpoint
            ]
            if missing_endpoint_fields:
                raise ProviderRegistryError(
                    "Endpoint for provider '{id}' missing fields: {fields}".format(
                        id=entry["id"], fields=", ".join(missing_endpoint_fields)
                    )
                )

            normalised_endpoints.append({
                key: endpoint[key] for key in endpoint
            })

        normalised_entry: Dict[str, Any] = {
            "id": entry["id"],
            "name": entry["name"],
            "region": entry["region"],
            "status": entry["status"],
            "endpoints": normalised_endpoints,
        }

        for optional_key in ("description", "capabilities", "contact", "notes"):
            if optional_key in entry:
                normalised_entry[optional_key] = entry[optional_key]

        normalised.append(normalised_entry)

    return {"metadata": metadata, "providers": normalised}


def _reset_provider_directory_cache() -> None:
    """Reset the directory cache - exposed for tests."""

    get_provider_directory.cache_clear()
