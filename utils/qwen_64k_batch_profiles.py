"""Stable Qwen 64K batch-profile configuration contract."""

from typing import Any, Final

QWEN_64K_BATCH_PROFILES: Final = {
    "safe": {"n_batch": 256, "n_ubatch": 128},
    "balanced": {"n_batch": 512, "n_ubatch": 256},
    "experimental": {"n_batch": 1024, "n_ubatch": 512},
}
DEFAULT_QWEN_64K_BATCH_PROFILE: Final = "balanced"


def normalize_qwen_64k_batch_profile(value: Any) -> str:
    """Normalize persisted/operator input without ever implicitly opting in."""

    if isinstance(value, str) and value in QWEN_64K_BATCH_PROFILES:
        return value
    return DEFAULT_QWEN_64K_BATCH_PROFILE
