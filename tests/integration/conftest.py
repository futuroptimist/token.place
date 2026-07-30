"""Per-test hang diagnostics for this integration suite.

This suite wires real threads/Events through ``_supervise_api_v1_inference``
and friends, and has caused at least one CI-only hang that was never
reproducible locally (all tests here normally finish in well under a
second). When a CI job hits its own hard timeout, the job is killed before
its log ever finishes uploading, so no diagnostic output survives at all.

If any single test stalls past a generous per-test budget, dump every
thread's stack to stderr and hard-exit, so the step fails on its own
(finalizing its log normally) with the actual stuck-thread traceback in it,
instead of silently disappearing into the job-level timeout.
"""
import faulthandler
import sys

import pytest

_PER_TEST_HANG_DIAGNOSTIC_SECONDS = 45


@pytest.fixture(autouse=True)
def _dump_stacks_and_exit_if_this_test_hangs():
    faulthandler.dump_traceback_later(
        _PER_TEST_HANG_DIAGNOSTIC_SECONDS, exit=True, file=sys.stderr
    )
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
