"""Unit tests for usage helper utilities in ``api.v1.routes``."""

from api.v1 import routes


def test_extract_message_content_for_usage_handles_various_payloads():
    """Ensure helper normalises different message content types."""

    # Non-dict payloads should return an empty string
    assert routes._extract_message_content_for_usage("hello") == ""

    # String content should be returned unchanged
    message_with_string = {"content": "hi there"}
    assert routes._extract_message_content_for_usage(message_with_string) == "hi there"

    # Structured content should be JSON encoded for downstream accounting
    message_with_structured_content = {"content": {"type": "tool", "result": [1, 2, 3]}}
    assert (
        routes._extract_message_content_for_usage(message_with_structured_content)
        == "{\"type\": \"tool\", \"result\": [1, 2, 3]}"
    )


def test_estimate_token_length_bounds_results_for_short_and_long_inputs():
    """The estimator should guard against zero tokens and ignore whitespace."""

    assert routes._estimate_token_length("") == 0
    assert routes._estimate_token_length("   \n  ") == 0

    # Short inputs should still register at least one token
    assert routes._estimate_token_length("hi") == 1

    # Longer inputs should roughly track the four-characters-per-token heuristic
    long_text = "abcd" * 5  # 20 characters -> 5 estimated tokens
    assert routes._estimate_token_length(long_text) == 5
