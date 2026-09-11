# PR #1831 bridge-isolation qualification manifest

Qualified on 2026-09-09 by static Git inspection only. This manifest is an
intake coordinate set, not Windows, installed-runtime, or physical-attempt
evidence.

## Verified source provenance

| Object | Identity |
| --- | --- |
| PR #1831 merge | `78acbe9af26b6347d10927cbd6f332192871c394` |
| Ordered merge parents | first `08f6196746af48b3d4827fac01880811bd497d6c`; second `9ce6c3ce433b3c9a8782b47205bd64a0d566d7e6` |
| Final PR head | `9ce6c3ce433b3c9a8782b47205bd64a0d566d7e6` (an ancestor and the second parent of the merge) |
| Merge/head tree | `86ab1337b7f4a2ffbba95f12a0a504afe8029d0a` |
| CI synthetic merge | `86faf039ef95c4a27381a8646d975fc2677b7ff1` |
| CI synthetic tree | `86ab1337b7f4a2ffbba95f12a0a504afe8029d0a` |
| Preserved baseline commit | `91f7d42f842ed1466ddeeb2a201b8d17726b651d` |
| Preserved baseline tree | `2fdf1322c588dff1daaccf1f912fe922a05ad91e` |
| Previously qualified PR #1813 merge | `b222033e24dfe373e06baefdd6b16d9e8ff5ea99` |

The actual merge and CI synthetic merge have different commit identities but
the same tree. An exit-code comparison of the two trees was clean. The diff
from the actual first parent to the merge modifies only
`tests/unit/test_desktop_compute_node_bridge.py` (`95` additions and `20`
deletions). The merge and final head both resolve that path to blob
`69e7e3297a63b4fc3767aca390423bce02f0ae75`; the baseline resolves it to
`fc7d5c7fbcba90c0de9739663350ce0eb69e0938`.

## Disposable candidate recipe and scope

Start from the preserved baseline and replace files with bytes read directly
from the following Git blobs (for example, with `git cat-file blob`):

| Path | Baseline blob | Replacement blob | Replacement SHA-256 |
| --- | --- | --- | --- |
| `tests/unit/test_relay_client.py` | `a29312c33759dbe1c303af01d66a7e3e614faad5` | `a37d894ffee1c42e5efe66640d5429b1f8249af3` | `3f078ec9cfd799c05bb55c9f9780dd2e5c260a4e6e211649c7eeca13e3f413b7` |
| `tests/unit/test_desktop_compute_node_bridge.py` | `fc7d5c7fbcba90c0de9739663350ce0eb69e0938` | `69e7e3297a63b4fc3767aca390423bce02f0ae75` | `c230a97e7835a23c0b7ed5195cf02e03e07fc7fcf1547d6be77eb7394bd20df2` |

The locally materialized candidate tree was
`44c256cd1c40d2751ba57f0677b54a21c1ad6e97`. Its status and name-status
contained exactly those two modified paths, both retained mode `100644`, and
`git diff --check` was clean. Comparing recursive `git ls-tree` records after
excluding the two replacements proved that all other `639` tracked entries,
including their object types, modes, blob/tree identities, and any submodule
entries, were identical to the baseline. Byte comparisons and `git hash-object`
reproduced both pinned replacement blob IDs.

The bridge file contains the same `183` named test functions as the baseline:
no original named bridge test was removed or added. Its complete pinned blob
therefore retains the original test definitions and parameterization while
applying the fixture changes. The relay file is exactly the previously
qualified blob from PR #1813, rather than a reconstruction of its patch, so its
existing heartbeat-isolation changes are retained.

## Static fixture and baseline compatibility findings

The following are findings from the merged source, checked against production
helpers at the preserved baseline:

- `_install_fake_runtime_module` now defaults dependency setup to success with
  a `**kwargs`-accepting fake. Tests that exercise provisioning still install
  their explicit success/failure/callback overrides after this helper, so those
  overrides win and remain observable.
- The missing-compute-runtime startup test now independently fakes dependency
  setup, context-runtime setup, context-profile loading, and re-exec. Its only
  intended failure is therefore the guarded `utils.compute_node_runtime`
  import, which is deterministic against the baseline helper call sequence.
- The lazy-export test installs sentinel modules and deletes only
  `get_model_manager`, `get_crypto_manager`, and `RelayClient` from the parent
  package dictionary before lookup. This exercises the baseline
  `utils.__getattr__` branches and leaves the eagerly imported `get_temp_dir`
  sentinel intact; pytest's monkeypatch teardown restores parent state.
- The child-path runtime class is installed before test-specific dependency and
  runtime provisioning fakes. The later explicit fakes therefore are not
  overwritten by the helper's defaults, while the fake runtime remains alive
  for the whole `run` call.
- The fatal child installs a process-local `requests.post` tripwire, a durable
  sentinel `config` module, and a persistent `get_config_lazy` fake. Its
  `_post_api_v1_request_control(**kwargs)` fake returns the baseline
  supervisor's expected `status=active` and `next_poll_seconds` fields without
  invoking transport.
- The fatal composition retains the real baseline
  `_wire_fatal_teardown_for_runtime` callback and real relay supervisor. It
  requires exact exit code `1`, rejects a traceback, rejects its unreachable
  post-supervisor marker, requires the privacy-safe `fatal_teardown` lifecycle
  marker, and requires that neither network nor configuration tripwire file was
  created.

No incompatibility was found between these fixture interfaces and the named
baseline production helpers. This is a static compatibility result, not an
execution result. In particular, the fatal test launches
`subprocess.run([sys.executable, child_script], ...)`; the operator-owned
Windows subprocess-admission guard must explicitly admit this bounded local
child before cumulative bridge execution. That external guard is not present
in this checkout and was not changed or exercised here.

## Reported evidence not re-qualified here

The task report says CI job `102311455284` ran synthetic merge
`86faf039ef95c4a27381a8646d975fc2677b7ff1`, with all `221` bridge cases
passing, unit totals of `4523 passed, 1 skipped`, and integration totals of
`310 passed, 2 skipped`; it also says the CI and `run_all_tests.sh` PR workflows
succeeded while the macOS Metal job was skipped. Those execution claims were
not rerun in this static task. The synthetic commit and its exact tree equality
with the actual merge were independently verified.

The task report also says the Greptile thread remains formally unresolved and
that later absent/preloaded-parent reproductions rejected the alleged leak.
That is recorded as report provenance, not upgraded to a resolved-thread claim;
no thread or other remote state was changed. The reported intermediate commit
`91b460cb7ce30620624316728f366a6c12f0d982` is intentionally not used for
provenance.

## Remaining gates

1. Intake the two pinned blobs into the operator-owned Windows derivative while
   retaining baseline HEAD/tree and verify the byte hashes and two-path scope.
2. Correct or explicitly authorize the Windows subprocess-admission guard for
   the bounded fatal-composition Python child.
3. Resume the cumulative Windows bridge contract execution only after that
   guard is qualified.
4. Refresh mutable staging and signed-runtime identity, then perform the final
   separately controlled non-consuming runtime preflight.
5. Keep any new physical attempt behind separate authorization.

`PHYSICAL_ATTEMPT_CONSUMED=false`
