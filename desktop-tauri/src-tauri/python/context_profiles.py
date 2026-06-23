"""Static context profile registry for the desktop compute-node bridge."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ContextProfile:
    profile_id: str
    display_label: str
    total_context_tokens: int
    default_output_reservation_tokens: int
    enabled: bool = True


DEFAULT_CONTEXT_TIER = "8k-fast"
CONTEXT_PROFILES = {
    "8k-fast": ContextProfile("8k-fast", "8K Fast", 8192, 512),
    "64k-full": ContextProfile("64k-full", "64K Full", 65536, 1024),
}


def resolve_context_profile(profile_id: str) -> Optional[ContextProfile]:
    profile = CONTEXT_PROFILES.get((profile_id or "").strip())
    if profile is None or not profile.enabled:
        return None
    return profile


def normalize_context_tier(profile_id: str) -> str:
    profile = resolve_context_profile(profile_id)
    return profile.profile_id if profile else DEFAULT_CONTEXT_TIER


def require_context_profile(profile_id: str) -> ContextProfile:
    profile = resolve_context_profile(profile_id)
    if profile is None:
        raise ValueError(f"unknown context profile: {profile_id}")
    return profile
