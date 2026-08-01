"""Canonical production budgets for API v1 inference completion."""

# The relay owns the inference-completion deadline and propagates its remaining
# budget in ciphertext-envelope routing metadata.
DEFAULT_INFERENCE_COMPLETION_TIMEOUT_SECONDS = 480.0

# Request initiators wait briefly beyond the relay-owned deadline so the relay
# can classify the timeout, propagate cancellation, and return that response.
INFERENCE_TIMEOUT_RESPONSE_GRACE_SECONDS = 5.0
DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS = (
    DEFAULT_INFERENCE_COMPLETION_TIMEOUT_SECONDS + INFERENCE_TIMEOUT_RESPONSE_GRACE_SECONDS
)
