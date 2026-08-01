"""Canonical production budgets for API v1 non-streaming inference."""

# The relay owns this completion deadline. Transport callers get a small grace
# period so the relay can classify the timeout and propagate cancellation.
DEFAULT_API_V1_INFERENCE_TIMEOUT_SECONDS = 480.0
API_V1_TIMEOUT_PROPAGATION_GRACE_SECONDS = 5.0
DEFAULT_API_V1_OUTER_TIMEOUT_SECONDS = (
    DEFAULT_API_V1_INFERENCE_TIMEOUT_SECONDS + API_V1_TIMEOUT_PROPAGATION_GRACE_SECONDS
)
