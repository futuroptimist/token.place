# PR #1831 bridge-isolation qualification manifest

Qualified on 2026-09-09 using repository object inspection only. No test suite,
desktop runtime, WebDriver, inference, benchmark, deployment, or physical attempt
was started.

## Verified source provenance

- PR merge commit: `78acbe9af26b6347d10927cbd6f332192871c394`.
- Ordered merge parents: first
  `08f6196746af48b3d4827fac01880811bd497d6c`, then
  `9ce6c3ce433b3c9a8782b47205bd64a0d566d7e6`.
- Final PR head `9ce6c3ce433b3c9a8782b47205bd64a0d566d7e6` is an ancestor of
  the merge. The final-head and merge trees are both
  `86ab1337b7f4a2ffbba95f12a0a504afe8029d0a`.
- The merge's bridge-test blob is
  `69e7e3297a63b4fc3767aca390423bce02f0ae75`, identical to the final
  PR head's bridge-test blob.
- First-parent-to-merge scope is one modified regular file (mode `100644`):
  `tests/unit/test_desktop_compute_node_bridge.py`, with 95 additions and 20
  deletions. `git diff --check` reports no whitespace errors.
- Successful-CI synthetic merge
  `86faf039ef95c4a27381a8646d975fc2677b7ff1` has the same ordered
  parents and the same tree, `86ab1337b7f4a2ffbba95f12a0a504afe8029d0a`,
  as the actual merge. A tree-to-tree `git diff --exit-code` is clean; this is
  direct object evidence rather than an inference from green CI.

## Verified merged fixture behavior

The final source diff establishes all of the following:

- The common fake-runtime installer defaults dependency provisioning to a
  successful result. Tests that exercise provisioning explicitly replace that
  default after installing the fake, so their chosen success/failure result and
  call-order assertions remain authoritative.
- The missing-compute-runtime startup test stubs dependency setup, context
  runtime setup, lazy context-profile loading, and re-exec before asserting the
  deterministic structured failure.
- The lazy `utils` export test installs sentinel modules, removes cached package
  attributes, then proves that `get_model_manager`, `get_crypto_manager`, and
  `RelayClient` are restored from the sentinels while `get_temp_dir` and
  `dir(utils)` behavior remain available.
- The child-model-path fixture is installed before provisioning overrides,
  preserving its child-lifetime configuration while allowing the test-specific
  dependency and runtime call recording to win.
- The fatal-teardown child installs durable network and configuration marker
  tripwires in the child interpreter, replaces request-control polling with a
  local active response, wires the real bridge fatal callback, and asserts exact
  exit status 1, absence of a traceback and unreachable marker, presence of the
  privacy-safe fatal lifecycle marker, and absence of both tripwire files.

The source diff also preserves every original bridge test function: AST
comparison found 183 original test functions, 183 candidate test functions, no
missing names, and no added names.

## Disposable derivative

The disposable checkout is rooted at preserved runtime-source commit
`91f7d42f842ed1466ddeeb2a201b8d17726b651d`, whose verified tree is
`2fdf1322c588dff1daaccf1f912fe922a05ad91e`. Its original bridge-test
blob is `fc7d5c7fbcba90c0de9739663350ce0eb69e0938`; its original relay-test
blob is `a29312c33759dbe1c303af01d66a7e3e614faad5`.

Exactly these regular-file (`100644`) replacements were applied from raw Git
blob bytes:

| Path | Replacement Git blob | SHA-256 of bytes | Provenance |
| --- | --- | --- | --- |
| `tests/unit/test_relay_client.py` | `a37d894ffee1c42e5efe66640d5429b1f8249af3` | `3f078ec9cfd799c05bb55c9f9780dd2e5c260a4e6e211649c7eeca13e3f413b7` | previously qualified PR #1813 merge `b222033e24dfe373e06baefdd6b16d9e8ff5ea99` |
| `tests/unit/test_desktop_compute_node_bridge.py` | `69e7e3297a63b4fc3767aca390423bce02f0ae75` | `c230a97e7835a23c0b7ed5195cf02e03e07fc7fcf1547d6be77eb7394bd20df2` | PR #1831 final head and merge |

A temporary-index reconstruction yields candidate tree
`44c256cd1c40d2751ba57f0677b54a21c1ad6e97`. Its raw tree diff from
the baseline contains only the two entries above, with unchanged modes. Thus
every other tracked path, executable bit, symlink, and submodule entry is
inherited byte-for-byte from the baseline. The relay replacement is exactly the
pinned previously qualified blob, so its heartbeat-isolation changes are
retained. The candidate's `git diff --check` is clean; its stat is 128 insertions
and 21 deletions across exactly two files.

## Static compatibility findings

There is no source-level incompatibility between the two replacement fixtures
and baseline production helpers. Baseline and PR-base copies of the inspected
production paths are identical. In particular, the baseline provides compatible
dependency setup (including keyword arguments), lazy configuration loading,
request-control replies, inference supervision and worker termination, context
helper loading, runtime refresh, and no-return fatal teardown wiring.

This finding is static compatibility only. It does not qualify Windows process
creation or execution. The known Windows subprocess-admission correction remains
an outstanding next step and was deliberately not repaired here.

## Reported evidence not independently elevated to source fact

The campaign report says Linux CI job `102311455284` passed all 221 bridge cases,
with 4523 passed/1 skipped unit and 310 passed/2 skipped integration results; it
also says the macOS Metal job was skipped. Those run-result claims were not
re-executed in this qualification. The Greptile thread remains formally open;
the later task report says absent/preloaded-parent reproductions reject its
alleged leak, but this manifest does **not** describe the thread as resolved and
does not alter its state.

## Remaining gates and state

The guarded Windows intake must independently verify the baseline HEAD/tree,
apply these two exact blob bytes, re-check their SHA-256 and candidate scope,
then complete the subprocess-admission correction before cumulative bridge
execution. Refreshing mutable staging/runtime identity and the final
non-consuming runtime preflight remain later gates. A physical run remains
separately authorization-gated.

The operator-owned Windows derivative was not accessed or changed. No external
state, deployment, release, workflow, issue, review thread, or preserved operator
evidence was changed. `PHYSICAL_ATTEMPT_CONSUMED=false`.
