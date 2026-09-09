# PR #1831 qualification manifest

Qualification date: 2026-09-09 UTC

## Provenance

| Coordinate | Verified value |
| --- | --- |
| PR | `futuroptimist/token.place#1831` |
| actual merge | `78acbe9af26b6347d10927cbd6f332192871c394` |
| actual merge tree | `86ab1337b7f4a2ffbba95f12a0a504afe8029d0a` |
| ordered merge parents | `08f6196746af48b3d4827fac01880811bd497d6c`, then `9ce6c3ce433b3c9a8782b47205bd64a0d566d7e6` |
| final PR head | `9ce6c3ce433b3c9a8782b47205bd64a0d566d7e6` (verified ancestor and second parent of the actual merge) |
| CI synthetic merge | `86faf039ef95c4a27381a8646d975fc2677b7ff1` |
| CI synthetic merge tree | `86ab1337b7f4a2ffbba95f12a0a504afe8029d0a` |
| preserved baseline commit | `91f7d42f842ed1466ddeeb2a201b8d17726b651d` |
| preserved baseline tree | `2fdf1322c588dff1daaccf1f912fe922a05ad91e` |
| disposable candidate tree | `44c256cd1c40d2751ba57f0677b54a21c1ad6e97` |

The actual and synthetic merge commits have the same ordered parents and byte-identical tree. The
synthetic object's tree coordinate was independently read from GitHub's Git database API because
GitHub no longer advertises its ephemeral merge ref. The actual merge object and tree were
inspected locally.

The actual first-parent-to-merge scope is exactly one mode-preserving modification:
`tests/unit/test_desktop_compute_node_bridge.py` (`95` insertions, `20` deletions). Its blob at both
the final head and actual merge is `69e7e3297a63b4fc3767aca390423bce02f0ae75`.

## Disposable baseline derivative

The follow-up candidate was assembled at the actual absolute location
`/tmp/token-place-pr1831-qualification-20260909T000000Z` from a detached checkout of the preserved
baseline. It remains available there at the time of this report. Both replacements were emitted
directly with `git cat-file blob`; no working copy or production path from current `main` was
copied.

| Path | Baseline blob | Replacement blob | Replacement SHA-256 |
| --- | --- | --- | --- |
| `tests/unit/test_relay_client.py` | `a29312c33759dbe1c303af01d66a7e3e614faad5` | `a37d894ffee1c42e5efe66640d5429b1f8249af3` | `3f078ec9cfd799c05bb55c9f9780dd2e5c260a4e6e211649c7eeca13e3f413b7` |
| `tests/unit/test_desktop_compute_node_bridge.py` | `fc7d5c7fbcba90c0de9739663350ce0eb69e0938` | `69e7e3297a63b4fc3767aca390423bce02f0ae75` | `c230a97e7835a23c0b7ed5195cf02e03e07fc7fcf1547d6be77eb7394bd20df2` |

A temporary-index `read-tree`/`update-index --cacheinfo`/`write-tree` construction produced the
candidate tree above. Recursive raw comparison against the baseline reports only those two paths,
both `100644 -> 100644`; therefore every other tracked path, mode, and gitlink is inherited
unchanged from the baseline tree. The baseline and candidate each contain 641 recursively tracked
entries: two are changed and the other 639 are byte- and mode-identical. Both replacement files
hash back to their pinned Git blob IDs.
AST inventory finds all `183` baseline bridge test functions and all `302` baseline relay-client
test functions still present, with no test-function additions or removals. The relay replacement is
the previously qualified PR #1813 blob from merge
`b222033e24dfe373e06baefdd6b16d9e8ff5ea99`.

## Source inspection and compatibility

Verified from the merged blob:

- The common fake-runtime installer now defaults dependency setup to `{"ok": "true"}`. Tests that
  explicitly replace `ensure_desktop_python_dependencies` do so after calling the installer, so
  their provisioning outcomes remain authoritative.
- The missing-compute-runtime startup case explicitly supplies successful dependency/runtime
  preflight, context-profile helpers, and a no-op re-exec before forcing the intended import
  failure. This makes its failure source deterministic.
- The lazy-export test installs sentinel leaf modules, removes cached parent exports, resolves each
  lazy export, and relies on `monkeypatch` to restore both `sys.modules` and parent-package state.
- The provisioning-order case installs its child-path runtime before applying explicit provisioning
  overrides, preserving those overrides while ensuring the fake child reports an existing path.
- The fatal child installs process-lifetime network and configuration tripwires, a deterministic
  request-control reply, and a permanently blocked inference worker. It exercises the real bridge
  fatal callback wiring and requires exact exit status `1`, no traceback, no post-supervisor marker,
  the fatal lifecycle marker, and no tripwire marker files.
- The relay fixture replaces request-control traffic with deterministic active replies, gates model
  generation until both control workers poll, rejects real HTTP, and asserts no owned worker thread
  remains.

These fixtures match the preserved baseline helper signatures and behavior: dependency preflight
returns a mapping keyed by `ok`; the bridge lazily loads context helpers after dependency preflight;
request-control replies accept `status` and `next_poll_seconds`; the supervisor wires a fatal
callback which uses `os._exit(1)`; and relay control uses the replaceable
`_post_api_v1_request_control` method. Static inspection found no source-level incompatibility and
made no repair.

The one intake constraint is environmental rather than a source mismatch: the fatal-composition
case intentionally creates a bounded Python child with `subprocess.run`. The Windows derivative's
subprocess guard must explicitly admit that known test child before cumulative bridge execution;
this qualification does not change that guard or execute the test.

## Review disposition and evidence boundaries

The public Greptile comment alleges that the lazy-export sentinel can leak through the parent
package. Its thread remains formally unresolved. Source inspection shows the final fixture removes
the cached parent exports and uses pytest `monkeypatch` restoration; the supplied task report also
claims absent/preloaded-parent reproductions rejected the leak. Those observations do not resolve
or mutate the review thread.

CI pass counts, the skipped macOS job, and reproductions described only in the task report remain
report evidence rather than newly executed evidence. No runtime-bearing suite was rerun. No
Windows, installed-runtime, GPU, inference, benchmark, deployment, or physical qualification is
claimed.

## Remaining Windows gates

1. Intake the two pinned blob byte streams into the operator-owned derivative while retaining
   baseline commit/tree identity and verify the candidate tree/scope/hashes above.
2. Correct the Windows subprocess-admission guard narrowly for the fatal-composition child.
3. Resume the cumulative Windows bridge contracts, then refresh mutable staging/runtime identity.
4. Execute the final non-consuming runtime preflight. Any new physical attempt remains separately
   authorization-gated.

## Mutation accounting and follow-up verification

Full historical compliance is **not** claimed. Commit
`130d3d156028f985d1a22d5aaebd313912b9a68b` and its publication as PR #1832 are historical changes
contrary to the original qualification-only instructions. Evidence establishing the original
checkout's status immediately before and after those historical actions is unavailable, so that
before/after state is **unverified**.

Mutation categories are deliberately kept distinct:

- **Original checkout:** At the start of this follow-up it was at
  `130d3d156028f985d1a22d5aaebd313912b9a68b` with an empty
  `git status --porcelain=v1 --untracked-files=all`. The historical commit already contained this
  manifest. The follow-up's final HEAD/status is reported in the task output rather than inferred
  here.
- **Disposable files and Git objects:** The isolated repository at the absolute location above has
  the two modified worktree files and no other status entries. Fetching the synthetic merge object
  and writing candidate tree `44c256cd1c40d2751ba57f0677b54a21c1ad6e97` affected only that
  disposable repository.
- **External changes:** PR #1832 and any associated remote Git objects already existed before this
  follow-up. This follow-up did not access or mutate the Windows derivative and did not perform
  Windows intake.

The exact future intake target is
`C:\Users\danie\tokenplace-long-context-evidence\windows-qualified-test-derivative-20260908T035220Z-5b4e31a5\harness-source`.
It was not accessed.

The requested static commands were run in the isolated repository. Actual outcomes were:

- `git status --porcelain=v1 --untracked-files=all` reported exactly the two modified test files.
- `git show --no-patch --format=raw 78acbe9af26b6347d10927cbd6f332192871c394`
  reported tree `86ab1337b7f4a2ffbba95f12a0a504afe8029d0a` and ordered parents
  `08f6196746af48b3d4827fac01880811bd497d6c`, then
  `9ce6c3ce433b3c9a8782b47205bd64a0d566d7e6`.
- `git diff --exit-code 86faf039ef95c4a27381a8646d975fc2677b7ff1^{tree}
  78acbe9af26b6347d10927cbd6f332192871c394^{tree}` exited 0 with no output.
- The two `git cat-file blob ... | sha256sum` commands produced, respectively,
  `3f078ec9cfd799c05bb55c9f9780dd2e5c260a4e6e211649c7eeca13e3f413b7` and
  `c230a97e7835a23c0b7ed5195cf02e03e07fc7fcf1547d6be77eb7394bd20df2`.
- `git diff --raw 91f7d42f842ed1466ddeeb2a201b8d17726b651d
  44c256cd1c40d2751ba57f0677b54a21c1ad6e97` reported only the two pinned paths,
  each with unchanged mode `100644` and the expected old/new blob IDs.
- `git diff --check` exited 0 with no output.
- `git diff --stat` reported two files, 128 insertions, and 21 deletions.

These are static findings only. Reported runtime execution evidence remains reported evidence and
was not reproduced. No runtime-bearing suite was rerun. The unresolved Greptile thread remains
formally unresolved. Windows intake, subprocess-admission correction, cumulative bridge execution,
and physical authorization remain outstanding.

`PHYSICAL_ATTEMPT_CONSUMED=false`
