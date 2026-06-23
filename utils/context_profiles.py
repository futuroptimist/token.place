"""Static desktop operator context-profile registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

DEFAULT_CONTEXT_TIER = "8k-fast"


@dataclass(frozen=True)
class ContextProfile:
    profile_id: str
    display_label: str
    total_context_tokens: int
    default_output_reservation: int
    enabled: bool = True


CONTEXT_PROFILES: Dict[str, ContextProfile] = {
    "8k-fast": ContextProfile(
        profile_id="8k-fast",
        display_label="8K Fast",
        total_context_tokens=8_192,
        default_output_reservation=512,
    ),
    "64k-full": ContextProfile(
        profile_id="64k-full",
        display_label="64K Full",
        total_context_tokens=65_536,
        default_output_reservation=1_024,
    ),
}


def resolve_context_profile(profile_id: str) -> ContextProfile:
    profile = CONTEXT_PROFILES.get(profile_id)
    if profile is None or not profile.enabled:
        raise ValueError(f"unknown or disabled context profile: {profile_id}")
    return profile


def normalize_context_tier(profile_id: object) -> str:
    return profile_id if isinstance(profile_id, str) and profile_id in CONTEXT_PROFILES and CONTEXT_PROFILES[profile_id].enabled else DEFAULT_CONTEXT_TIER
