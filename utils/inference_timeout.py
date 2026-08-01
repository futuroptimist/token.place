"""Canonical production budgets for API v1 non-streaming inference."""

# The relay owns the inference-completion deadline. Outer requesters get a small
# response-propagation window so relay expiry/cancellation can arrive first.
DEFAULT_INFERENCE_TIMEOUT_SECONDS = 480.0
INFERENCE_RESPONSE_GRACE_SECONDS = 5.0
DEFAULT_INFERENCE_TRANSPORT_TIMEOUT_SECONDS = (
    DEFAULT_INFERENCE_TIMEOUT_SECONDS + INFERENCE_RESPONSE_GRACE_SECONDS
)
