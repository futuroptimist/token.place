# Valkey staging validation and production cutover

**Status:** proposed rollout plan; no shadow implementation or deployment authorization.

This plan adds one narrow exception to the
[atomic-transitions ADR](valkey_relay_state_atomic_transitions_adr.md): staging may duplicate
store operations into a disposable, non-serving Valkey shadow to measure semantic parity.
Memory remains the sole authority in that experiment and in production until an independently
approved cutover. Production never runs this shadow or dual-writes. The exception does not permit
cross-major migration, two authorities, or promotion of shadow data into service.

The target remains API v1, non-streaming, relay-blind E2EE. Arbitrary self-hosted relays remain
supported: operators can retain supported single-process memory mode and their own endpoints.
This plan neither requires a hosted relay nor silently changes existing installations' backend.

## Stages and authority

| Stage | Serving authority | Valkey role | Exit evidence |
|---|---|---|---|
| Baseline | One memory relay | None | Recorded workload and overhead baseline |
| Staging shadow | The same memory relay | Isolated, disposable comparison state | Valid epochs with explained comparisons and bounded overhead |
| Staging cutover rehearsal | One Valkey relay after memory drain and stop | Fresh authoritative namespace | Recovery, rollback, compatibility and client re-registration evidence |
| Production cutover | Memory until stopped; then one Valkey relay | Fresh production authority | Separate operator go/no-go and monitored acceptance |
| Later HA scale-out | Valkey | Shared authority | Independent concurrency, limiter, durability and failover gates |

The staging experiment requires a separately reviewed implementation, disabled by default, with
an explicit staging environment guard and a kill switch. An attempted production enablement must
fail configuration validation. This document does not add configuration flags or claim they exist.

## Staging-only dual-write means an operation shadow

### Isolation and bootstrap

Use a dedicated staging Valkey service, credentials and resource limits where possible. At minimum,
use a fresh validated environment/cluster namespace for **each experiment epoch**, separate from
all serving namespaces, with ACLs restricted to that namespace. Shared infrastructure must have
reviewed resource isolation and may not place production availability at risk. Record the exact
relay commit, backend configuration, schema/writer revisions and complete script-digest map in the
epoch's non-sensitive evidence manifest. Never point serving clients or compute nodes at the shadow.

Prefer starting an epoch with both stores empty under a staging maintenance window: stop admission,
drain and stop the previous memory relay, create the empty shadow namespace, start a fresh memory
relay with capture enabled before the first registration, then let staging nodes re-register.
Joining a populated memory store is not a valid baseline. A future alternative must specify and
prove a consistent snapshot plus sequence handoff: fence mutations, capture the complete bounded
state and comparison mappings at sequence S, install it through a reviewed compatible importer,
then replay every operation after S exactly once. No such importer is supplied by this plan.

Stop and discard an epoch on restart, lost capture, or incompatible configuration. Delete only its
known isolated namespace through a reviewed cleanup procedure; never issue a global flush. A fresh
epoch must bootstrap again. Shadow namespaces are never promoted, restored into production, or
used as a fallback when memory fails.

### Capture the store's actual order

Capture validated, typed `RelayStateStore` calls at the authority boundary, not HTTP requests.
Assign a monotonically increasing sequence in the memory store's actual serialization order,
covering the operation, its normalized input, and its result or fixed failure class. The future
adapter must establish that ordering under the same synchronization as the authoritative operation;
recording only request start/completion order is insufficient. Memory's result alone goes to callers.

Replay the complete ordered stream through one bounded shadow executor. Include registration,
renewal, scheduler updates, selection/reservation, enqueue, claim/reclaim, control/acknowledgement,
progress, response, retrieval/acknowledgement, cancellation, expiry and node-removal continuation.
Include reads that reap, expire, acknowledge or otherwise change lifecycle state, and background
cleanup invocations. Audit every method in the [store protocol](../../relay_state_store.py) for
these effects; copying only writes would compare different histories. A shadow background worker
must not independently advance state outside this sequence. Internal time-based behavior of the
Valkey operations still applies and needs the clock treatment below.

The executor does not dispatch compute, deliver responses, make client requests, renew external
leases, send wakeups outside its namespace, change serving readiness, or emit production outcome
metrics. Shadow claims are store transitions only. Use an isolated metrics registry with fixed
comparison labels; do not double-count outcomes in the memory relay's serving registry.

### Compare semantics at explicit watermarks

Compare each operation's fixed result class and normalized semantics. At bounded checkpoints,
compare both stores at the **same completed sequence watermark**. This requires either a bounded
capture barrier with side-effect-free snapshots, or reviewed immutable snapshots indexed by
sequence. Do not compare a caught-up Valkey snapshot with newer live memory state. Comparison
inspection must not introduce unrecorded cleanup calls. Include logical lifecycle, ownership,
generation, eligibility, scheduler choice/cursor effects, queue order, capacity accounting,
response/progress presence, terminal outcome and tombstone retention, not physical key layout.

Reservation and acknowledgement tokens differ between independent backends. Maintain a bounded,
transient in-memory mapping between corresponding successful token issuances, bound to canonical
identity, purpose and lifecycle. Translate only a token proven to be the matching issued token;
never replace arbitrary caller input with a currently valid shadow token. Wrong, missing,
cross-request, stale, replayed and expired inputs must keep their rejection or idempotent semantics.
Retain mappings only for the required bounded lifecycle/replay window, including consumed-token
retries, then erase them. Map other backend-generated opaque identities only where the contract
permits equivalence; do not normalize away different scheduler choices, generations or outcomes.
A missing mapping makes the comparison inconclusive and requires investigation, not a successful
match. Tokens and mappings never enter diagnostics or durable replay files.

Memory's process clock and Valkey's `TIME` are different authorities. Measure replay lag and clock
uncertainty, and define a conservative exclusion window around reservation, claim, request, response
and retention deadlines before collecting evidence. Different results inside that window are
**inconclusive**, not parity successes; investigate their frequency and rerun controlled boundary
cases. Do not ignore state divergence propagated beyond an excluded boundary: stop that comparison
epoch or restart from a proven consistent baseline. Unexpected differences outside the window are
mismatches. Do not inject client-controlled time into production Lua to make replay agree. Controlled
clocks belong in isolated test harnesses; real-time boundary behavior needs independent tests.

### Bounded overhead, failure and privacy budgets

Before enabling capture, the experiment owner must publish numeric limits for queue entries/bytes,
operation size, token-map size, replay lag, command timeout, total retry time, checkpoint pause,
and incremental serving p95/p99 latency, CPU and memory. Also define a collection duration and
minimum operation counts for each lifecycle class. These are approval inputs to be measured against
a same-workload baseline, not measurements supplied by this document.

Capture must not wait for Valkey. A full queue, oversized event, timeout, failed shadow operation,
sequence gap, mapping loss or failed checkpoint invalidates the affected epoch's **clean-parity
claim**; count it explicitly and stop shadow replay pending reset/triage. Never drop an event and
continue claiming agreement. Preserve memory serving behavior, trip the kill switch if overhead
exceeds budget, and report the epoch as failed/incomplete. Ambiguous shadow mutations must not be
blindly retried; any recovery needs proof of exactly which sequence was applied. Otherwise reset.
Zero tolerated unexplained semantic mismatches and zero unaccounted gaps are acceptance requirements;
a bounded failure experiment may intentionally cause failures but cannot count as clean parity.

Do not retain raw prompts, credentials, HTTP requests/bodies, headers, ciphertext or raw identifiers
in event logs, traces, artifacts or diagnostic samples. The executor may transiently hold only the
validated operation material needed to exercise the normal store contract, including ciphertext
and token inputs, under strict byte and lifetime bounds. The isolated Valkey store may hold the
ADR-allowed envelopes for their normal lifecycle TTL only; this is not a diagnostic replay archive.
Disable command/value tracing and persistent shadow payload archives. Use synthetic staging traffic
for sensitive-input and failure cases. Export only fixed-label aggregate counts, result categories,
lag/latency distributions and resource usage. No user/model/node/request IDs, raw keys, payload
hashes, token values, exception strings or unbounded labels in collected diagnostics. Verify deletion
at epoch end, including any configured staging persistence files; avoid retaining payload backups.

## Collection, triage and qualification

Run a baseline with shadow disabled, then equivalent controlled workloads with it enabled. Cover
normal registration through acknowledgement, idle/busy nodes, heterogeneous scheduler eligibility,
capacity limits, repeated identities, invalid/replayed tokens, cancellation, expired work,
unregister and node-ID reuse. Run each workload long enough to exercise configured retention and
cleanup windows. Record operation counts, comparable/inconclusive/mismatch counts, valid watermark
coverage, queue high-water marks, gaps, timeouts, replay lag and overhead against the declared
budgets. A low-traffic epoch without the required lifecycle coverage is insufficient.

For a mismatch, first classify capture completeness, bootstrap correctness, token mapping and clock
boundaries. Then isolate a synthetic minimal sequence and check the intended store contract; memory
is the serving authority, not automatically the correct implementation. Fix the backend or comparator
only after review, add an appropriate regression, and collect fresh evidence on the new exact commit.
Do not change normalization or widen exclusions simply to hide a disagreement. Privacy violations,
duplicate external side effects and serving impact stop the experiment immediately.

Existing tests inform future validation; listing them here does **not** claim they were run for
this documentation change or that they qualify the proposed shadow:

| Existing coverage source | Proposed extension or retained gate |
|---|---|
| [Memory lifecycle and scheduler contract](../../tests/unit/test_relay_state_store.py) | Replay registration, ownership, lifecycle, retention, eligibility, fairness, reservation/token retry and capacity effects; prove failed operations are compared |
| [Backend selection contract](../../tests/unit/test_relay_state_store_selection.py) | Retain memory defaults, exact configuration validation, redacted failures and no automatic fallback |
| [Route adapters](../../tests/unit/test_relay_state_store_routes.py) | Prove only memory results reach callers, no duplicate dispatch, unchanged readiness and isolated metrics |
| [Valkey unit tests](../../tests/unit/test_valkey_relay_state.py) | Manifest/digest rejection, fixed errors and bounded adapter failures |
| [Real-Valkey integration tests](../../tests/integration/test_valkey_relay_state.py) | Lifecycle/TTL, node-transition capacity and retained controls, malformed-state atomicity, fairness, former-owner fences and namespace isolation |

New shadow-specific tests must cover bootstrap fencing, ordering under concurrent callers, reads
with lifecycle effects, sequence gaps, queue/byte overflow, executor crashes, ambiguous timeout,
watermark skew, token-map exhaustion, clock boundaries, kill-switch recovery and forbidden-data
leakage. Verify that production configuration cannot enable it. These are future implementation
gates, not additions made by this documentation PR.

A staging report must link the exact commits, configuration and schema manifests, predefined budgets,
workload/coverage matrix, all excluded/failed epochs, aggregate evidence, triage decisions and owner
sign-off. Clean shadow parity alone does not qualify HA: serial replay cannot prove distributed
atomicity, concurrent ordering, multi-relay behavior, or durability. Independently prove the ADR's
real-Valkey contract, cross-relay lifecycle, simultaneous claims/admission, cancellation races,
restart recovery, primary loss/promotion, network interruption, stale connection handling and
rolling compatibility gates. Test the intended Sentinel/persistence topology, AOF/RDB recovery and
acknowledged-write-loss behavior under asynchronous replication. A standalone Valkey is not HA.

Shared Flask/control-plane rate limiting is a separate implementation and verification scope; this
shadow does not mirror or qualify quota decisions. Shared limiter correctness and fail-closed outage
behavior remain mandatory before multiple relay processes/workers serve production.

## Production cutover: drain, stop, re-register

Require a separate operator-approved change window after a staging cutover rehearsal. The approval
must name the exact release/digests, target namespace and topology, qualified client/compute versions,
readiness evidence, observation window, abort thresholds, recovery owner and accepted state-loss
policy. This PR grants none of that operational authorization. Memory remains production authority
until that change window; there is no production shadow collection.

1. **Prepare a fresh production namespace.** Never reuse a shadow namespace. Validate resource bounds,
   no-eviction policy, persistence, Sentinel discovery and separate discovery/data ACLs, fixed error
   behavior, and exact schema/script compatibility. Keep it out of service until the cutover.
2. **Fence new admission and drain memory.** Stop new selections/reservations/enqueues while allowing
   already accepted work to poll, complete, retrieve and acknowledge. Expire unused reservations.
   Wait for queued/claimed work and pending node transitions to finish, and account for unacknowledged
   responses, terminal records and controls through their required retention windows. A drain deadline
   alone is not proof of an empty lifecycle. If anything cannot be drained, abort and resume memory,
   or obtain explicit operator acceptance to cancel/expire and lose that state before proceeding.
3. **Stop and fence the old relay.** Prevent old workers or automatic restarts from accepting traffic.
   There must be no overlap of memory and Valkey serving authorities. Capture only aggregate drain
   evidence. Stopping memory destroys its registrations, cursors, dedup history and any residual
   in-flight/retained state; do not promise seamless migration or preservation of old retry identities.
4. **Start one compatible Valkey relay.** Validate bounded ping, writable-primary role, manifest and
   the entire expected digest map before readiness. Let compute nodes re-register; readiness is not
   proof of compute capacity. Verify eligible capacity and a synthetic API-v1 E2EE lifecycle including
   response acknowledgement before admitting normal traffic. Old tokens/claims cannot authorize new
   work. Clients must handle failed/expired old requests explicitly; blindly resubmitting uncertain
   work may duplicate computation, so cutover is not exactly once.
5. **Observe before expanding.** Check error rates, lifecycle completion, capacity, latency, cleanup,
   resource budgets and recovery evidence against the approved thresholds. Keep one relay until
   concurrency, shared-limiter and HA gates pass. Scale only through a separate approved action.

[PR #1910](https://github.com/futuroptimist/token.place/pull/1910) merged into main as
[`843bfff4cd380200c37fc9b7eda0275d561baad7`](https://github.com/futuroptimist/token.place/commit/843bfff4cd380200c37fc9b7eda0275d561baad7)
from head `491d51ccc0968b423b1c3aede8b37ce6979676bf`. Its opt-in external Valkey/Sentinel chart
contract and tests are available for qualification; merging it does not deploy or qualify a live
topology. It changes the reviewed `node_transition_v1` digest.
Manifests carrying the predecessor map remain incompatible. Do not rewrite a live manifest during
initialization or assume mixed-version rolling compatibility. Verify the
full compiled digest map for the exact release chosen for cutover. Reusing any nonempty authoritative
namespace requires the ADR's explicit stopped-namespace compatibility/migration procedure; otherwise
use a fresh compatible namespace after approved drain. Reader revision overlap alone is insufficient.

## Rollback and state-loss decisions

Before stopping memory, abort by disabling the shadow (staging only) or reopening admission to the
same memory authority. After memory stops, its state is gone; restarting memory is not restoration.
If a fresh Valkey relay has not accepted any work, operators may return to a fresh single memory
relay only after fencing Valkey, explicitly accepting the lost registrations/history and arranging
re-registration. Verify that no admissions occurred rather than assuming the window was empty.

After Valkey has accepted work, the normal rollback is **one compatible relay retaining Valkey and
its namespace**, as required by the ADR. Roll back application code only if its entire script map
and reader/writer compatibility pass; an incompatible predecessor cannot serve the same namespace.
Fail closed and recover Valkey if needed. Never automatically fall back to memory on store outage.

Returning to memory after accepted Valkey work is a separate exceptional maintenance decision,
not routine rollback: fence admissions, drain through response/terminal retention, stop all Valkey
writers/serving readers, and explicitly approve any unrecoverable state loss before starting fresh
memory and re-registering nodes. If Valkey is unavailable, a completed drain cannot be asserted;
keep service stopped or obtain an explicit loss decision. Lost responses and dedup history cannot
be reconstructed from shadow diagnostics. A client retry may require a new lifecycle and may cause
duplicate computation. Do not replay late responses or old claims across namespaces. Retain the
failed authoritative namespace only under the approved recovery/privacy policy; never merge it into
a new authority while either is serving.
