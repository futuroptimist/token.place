"""Progress browser tests must select relay-only fixtures in every invocation."""

from types import SimpleNamespace

import pytest

from tests.conftest import _is_focused_relay_landing_chat_request


PROGRESS_PREFIX = (
    "tests/e2e/test_ui.py::"
    "test_landing_chat_uses_api_v1_only_non_streaming_encrypted_progress_"
)
PROGRESS_NODEIDS = [PROGRESS_PREFIX + "native_ui_semantics"] + [
    PROGRESS_PREFIX + f"terminal_lifecycle_cleanup[{exit_kind}]"
    for exit_kind in (
        "structured_failure", "cancellation", "timeout", "failover", "pagehide", "teardown"
    )
]
REGISTRATION_NODEID = "tests/e2e/test_ui.py::test_send_message"


def fixture_request(nodeid, invocation_args, selected_nodeids):
    return SimpleNamespace(
        node=SimpleNamespace(nodeid=nodeid),
        session=SimpleNamespace(
            items=[SimpleNamespace(nodeid=item) for item in selected_nodeids]
        ),
        config=SimpleNamespace(
            invocation_params=SimpleNamespace(args=invocation_args)
        ),
    )


@pytest.mark.parametrize("nodeid", PROGRESS_NODEIDS)
@pytest.mark.parametrize("invocation", ["direct", "broader"])
def test_progress_uses_relay_only_setup(nodeid, invocation):
    args = (
        (nodeid,)
        if invocation == "direct"
        else ("tests/e2e/test_ui.py", "-k", "landing_chat and not real_inference")
    )
    # Include another selected test so a session-wide allowlist shortcut cannot
    # conceal a missing progress node ID in the classifier's per-test decision.
    request = fixture_request(nodeid, args, [nodeid, REGISTRATION_NODEID])
    assert _is_focused_relay_landing_chat_request(request)


def test_registration_dependent_test_does_not_use_relay_only_setup():
    request = fixture_request(
        REGISTRATION_NODEID,
        ("tests/e2e/test_ui.py", "-k", "landing_chat and not real_inference"),
        [REGISTRATION_NODEID, *PROGRESS_NODEIDS],
    )
    assert not _is_focused_relay_landing_chat_request(request)
