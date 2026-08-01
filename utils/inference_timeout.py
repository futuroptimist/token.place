"""Canonical production budgets for non-streaming API v1 inference."""

# The relay owns the inference-completion deadline. Outer request initiators get
# a small response-propagation window so they cannot cancel a valid relay result
# while its terminal ciphertext is travelling back to the caller.
DEFAULT_INFERENCE_TIMEOUT_SECONDS = 480.0
INFERENCE_RESPONSE_GRACE_SECONDS = 5.0
DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS = (
    DEFAULT_INFERENCE_TIMEOUT_SECONDS + INFERENCE_RESPONSE_GRACE_SECONDS
)
