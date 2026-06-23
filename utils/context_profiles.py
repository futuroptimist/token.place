"""Static context profiles for token.place desktop operators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


DEFAULT_CONTEXT_TIER = "8k-fast"


@dataclass(frozen=True)
class ContextProfile:
    profile_id: str
    display_label: str
    total_context_tokens: int
    default_output_reservation_tokens: int
    enabled: bool = True


_CONTEXT_PROFILES = (
    ContextProfile("8k-fast", "8K Fast", 8192, 1024, True),
    ContextProfile("64k-full", "64K Full", 65536, 1024, True),
)

CONTEXT_PROFILES: Dict[str, ContextProfile] = {
    profile.profile_id: profile for profile in _CONTEXT_PROFILES
}


def get_context_profile(profile_id: Optional[str]) -> ContextProfile:
    profile = CONTEXT_PROFILES.get(profile_id or DEFAULT_CONTEXT_TIER)
    if profile is None or not profile.enabled:
        raise ValueError(f"unknown or disabled context profile: {profile_id}")
    return profile


def normalize_context_tier(profile_id: Optional[str]) -> str:
    profile = CONTEXT_PROFILES.get(profile_id or DEFAULT_CONTEXT_TIER)
    if profile is None or not profile.enabled:
        return DEFAULT_CONTEXT_TIER
    return profile.profile_id


def apply_context_profile(manager: object, profile_id: Optional[str]) -> ContextProfile:
    profile = get_context_profile(profile_id)
    config = getattr(manager, "config", None)
    if config is not None and hasattr(config, "set"):
        config.set("model.context_size", profile.total_context_tokens)
    elif isinstance(config, dict):
        config["model.context_size"] = profile.total_context_tokens
    setattr(manager, "context_tier", profile.profile_id)
    setattr(manager, "context_window_tokens", profile.total_context_tokens)
    setattr(manager, "default_output_reservation_tokens", profile.default_output_reservation_tokens)
    return profile
