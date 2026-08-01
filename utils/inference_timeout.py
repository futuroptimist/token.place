"""Shared production budgets for non-streaming API v1 inference."""

API_V1_INFERENCE_COMPLETION_TIMEOUT_SECONDS = 480.0
# Outer request initiators need one final relay polling/response round after the
# authoritative inference deadline terminalizes and cancels compute work.
API_V1_RESPONSE_PROPAGATION_GRACE_SECONDS = 5.0
API_V1_OUTER_REQUEST_TIMEOUT_SECONDS = (
    API_V1_INFERENCE_COMPLETION_TIMEOUT_SECONDS
    + API_V1_RESPONSE_PROPAGATION_GRACE_SECONDS
)
