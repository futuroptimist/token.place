"""Stable Qwen 64K batch-profile configuration contract."""

from typing import Any, Dict, Tuple

QWEN_64K_BATCH_PROFILE_SAFE = "safe"
QWEN_64K_BATCH_PROFILE_BALANCED = "balanced"
QWEN_64K_BATCH_PROFILE_EXPERIMENTAL = "experimental"
DEFAULT_QWEN_64K_BATCH_PROFILE = QWEN_64K_BATCH_PROFILE_BALANCED

QWEN_64K_BATCH_PROFILES: Dict[str, Tuple[int, int]] = {
    QWEN_64K_BATCH_PROFILE_SAFE: (256, 128),
    QWEN_64K_BATCH_PROFILE_BALANCED: (512, 256),
    QWEN_64K_BATCH_PROFILE_EXPERIMENTAL: (1024, 512),
}


def normalize_qwen_64k_batch_profile(value: Any) -> str:
    """Normalize persisted input without ever implicitly opting into experimental."""

    return value if isinstance(value, str) and value in QWEN_64K_BATCH_PROFILES else DEFAULT_QWEN_64K_BATCH_PROFILE


def qwen_64k_batch_values(value: Any) -> Tuple[int, int]:
    return QWEN_64K_BATCH_PROFILES[normalize_qwen_64k_batch_profile(value)]
