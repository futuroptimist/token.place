"""Stable Qwen 64K batch-profile configuration contract."""
from typing import Any, NamedTuple


class Qwen64KBatchProfile(NamedTuple):
    profile_id: str
    n_batch: int
    n_ubatch: int


QWEN_64K_BATCH_PROFILE_SAFE = "safe"
QWEN_64K_BATCH_PROFILE_BALANCED = "balanced"
QWEN_64K_BATCH_PROFILE_EXPERIMENTAL = "experimental"
DEFAULT_QWEN_64K_BATCH_PROFILE = QWEN_64K_BATCH_PROFILE_BALANCED

QWEN_64K_BATCH_PROFILES = {
    QWEN_64K_BATCH_PROFILE_SAFE: Qwen64KBatchProfile("safe", 256, 128),
    QWEN_64K_BATCH_PROFILE_BALANCED: Qwen64KBatchProfile("balanced", 512, 256),
    QWEN_64K_BATCH_PROFILE_EXPERIMENTAL: Qwen64KBatchProfile("experimental", 1024, 512),
}


def normalize_qwen_64k_batch_profile(value: Any) -> str:
    """Normalize missing/malformed values without ever implicitly opting in."""
    return value if isinstance(value, str) and value in QWEN_64K_BATCH_PROFILES else DEFAULT_QWEN_64K_BATCH_PROFILE


def get_qwen_64k_batch_profile(value: Any) -> Qwen64KBatchProfile:
    return QWEN_64K_BATCH_PROFILES[normalize_qwen_64k_batch_profile(value)]


def qwen_64k_batch_downshift_ids(value: Any) -> tuple[str, ...]:
    normalized = normalize_qwen_64k_batch_profile(value)
    ordered = (
        QWEN_64K_BATCH_PROFILE_EXPERIMENTAL,
        QWEN_64K_BATCH_PROFILE_BALANCED,
        QWEN_64K_BATCH_PROFILE_SAFE,
    )
    return ordered[ordered.index(normalized):]
