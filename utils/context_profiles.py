"""Static context profile registry for token.place operator runtimes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

DEFAULT_CONTEXT_TIER_ID = "8k-fast"


@dataclass(frozen=True)
class ContextProfile:
    profile_id: str
    display_label: str
    total_context_tokens: int
    default_output_token_reservation: int
    enabled: bool = True


CONTEXT_PROFILES: Dict[str, ContextProfile] = {
    "8k-fast": ContextProfile(
        profile_id="8k-fast",
        display_label="8K Fast",
        total_context_tokens=8192,
        default_output_token_reservation=512,
    ),
    "64k-full": ContextProfile(
        profile_id="64k-full",
        display_label="64K Full",
        total_context_tokens=65536,
        default_output_token_reservation=1024,
    ),
}


def get_context_profile(profile_id: Optional[str]) -> Optional[ContextProfile]:
    if not isinstance(profile_id, str):
        return None
    profile = CONTEXT_PROFILES.get(profile_id.strip())
    if profile is None or not profile.enabled:
        return None
    return profile


def normalize_context_tier(profile_id: Optional[str]) -> str:
    profile = get_context_profile(profile_id)
    return profile.profile_id if profile is not None else DEFAULT_CONTEXT_TIER_ID


def require_context_profile(profile_id: Optional[str]) -> ContextProfile:
    profile = get_context_profile(profile_id)
    if profile is None:
        raise ValueError(f"unknown or disabled context profile: {profile_id!r}")
    return profile
