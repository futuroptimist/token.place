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


# Context tiers intentionally use static, duplicated profile constants instead of runtime
# codegen/manifest loading. Keep these IDs and token counts synchronized with
# desktop-tauri/src-tauri/src/context_profiles.rs and desktop-tauri/src/App.tsx.
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
    """Select the operator runtime n_ctx without changing API v1 admission policy."""
    profile = get_context_profile(profile_id)
    # Deliberately leave relay_client.py admission/request-size limits untouched:
    # Context tier selection warms one operator runtime profile, while exact long-context admission
    # and tier-aware relay selection are later API v1 policy work.
    config = getattr(manager, "config", None)
    if config is not None and hasattr(config, "set"):
        config.set("model.context_size", profile.total_context_tokens)
    elif isinstance(config, dict):
        config["model.context_size"] = profile.total_context_tokens
    setattr(manager, "context_tier", profile.profile_id)
    setattr(manager, "context_window_tokens", profile.total_context_tokens)
    setattr(manager, "default_output_reservation_tokens", profile.default_output_reservation_tokens)
    return profile
