# Authenticated inference: review and deterministic vector plan

Status: review preparation only. No executable vectors, new dependencies or
runtime implementation accompany this plan. The
[candidate protocol](authenticated-inference-protocol.md) is not qualified
security, and documentation CI cannot qualify it. The existing
[compute trust design](verified-compute-trust.md) remains the threat-model and
cross-client coordination baseline.

## 1. What is settled, candidate and blocking

| Class | Meaning in this packet |
| --- | --- |
| Settled product requirements | Independently verified user/admin-supplied public operator bundles; explicit plaintext-recipient trust; arbitrary self-hosted relays; no required project-operated offering, central account or centralized live authorization lookup. Offline use is limited by current local authority, with no minimum disconnected-time promise. Verified mode fails closed and requires independently trusted client delivery. |
| Candidate technical choices | Exact profile/schema, single delegated issuer, root-wide cumulative snapshots, strict JCS/base64url, Ed25519 transcript signatures inside fresh single-message HPKE contexts, replay/fencing state machine, and storage/library shortlist. These remain subject to review and revision. |
| Blocking engineering decisions | P01-P10 below, including all numeric lifecycle/resource bounds, complete API mappings, named specialists, trust persistence/recovery, qualification pins and execution authority. No row is implicitly resolved by merging this document. |

All clients must agree before shipping: token.place compute, Python API client,
desktop, browser clients including DSPACE, and Gabriel's trusted local adapter.
API v1 stays non-streaming; progress is telemetry only. Existing installations
without an explicit saved trust-mode choice pause dispatch. Compatibility remains
explicit encrypted API v1; never use a failed verification as consent to downgrade,
enable API v2, use deprecated routes or send plaintext to a relay.

The current repository baseline for this packet is
`a42a58f824f21d5b4c51a22db838116899d49983`. Read
[compute_provider.py](../../api/v1/compute_provider.py),
[crypto_manager.py](../../utils/crypto/crypto_manager.py),
[requirements.txt](../../requirements.txt) and the trust design when checking
implementation claims. The baseline pins `cryptography==46.0.1`; that observed
existing dependency does not approve it for this new composition. Current clients
still lack the proposed independent compute authentication.

## 2. Blocking decision ledger

Every row is **OPEN**. Roles identify required expertise, not assigned reviewers.
Before G1 passes, replace each unassigned ownership record with a named accountable
owner and independent reviewer, their exact reviewed revision, decision/rationale,
evidence links, unresolved comments and explicit acceptance. Do not invent names
or treat an AI review as independent cryptographic qualification. A material change
to an accepted contract invalidates affected acceptance until re-reviewed.

| ID | Required owners (all unassigned) | Concrete closure evidence |
| --- | --- | --- |
| P01 | Protocol maintainer; independent cryptographic specialist; all client maintainers | Accept or revise byte framing, key IDs/fingerprints, profile identifiers, Ed25519 validation, strict parsing and sign-then-encrypt composition. Trace request/recipient/result/error/progress/cancel binding, key-purpose separation, reflection, key substitution, unknown-key share and error-oracle risks. Resolve every ambiguous byte. |
| P02 | API/compute maintainer; Python/desktop/browser/DSPACE/Gabriel maintainers | Inventory every current request, response, error, progress and cancellation variant. Freeze exact API body, model/tier/option/result/error registries, decimal ranges/defaults, payload limits and rejection rules. Show no semantic field is omitted or unsigned; map old fields and capabilities to the new envelope without lossy conversion. |
| P03 | Protocol/lifecycle maintainer; resource/clock specialist; client owners | Publish justified finite numbers for every limit listed below, exact half-open boundaries and uncertainty algorithm. Demonstrate no clock/skew/suspend/reboot condition extends signed validity; quantify availability tradeoffs without promising offline duration. |
| P04 | Enrollment/recovery maintainer; independent security reviewer; client delivery owners | Freeze bundle and local policy schemas, independent fingerprint ceremony, managed-policy precedence, safe URL retrieval and cancellation behavior. Demonstrate independent client origin/installer/update trust and root-compromise recovery. No relay or mutable verifier may repair missing trust. |
| P05 | Operator lifecycle reviewer; trust-store persistence owners; security reviewer | Specify root/status signing custody and cadence, cumulative revocation handling, generation/epoch persistence, atomic policy replacement and same-epoch conflict recovery. Trace update interruption, issuer coordination, rotation overlap, state loss, restore and root replacement. |
| P06 | Compute/replay maintainer; client persistence owners; independent security reviewer | Define authoritative acceptance-domain identity, atomic reservation/capacity/grant/terminal boundaries, monotonic progress and client acceptance. Prove duplicate/conflicting retries and stale workers cannot acquire fresh execution/publication authority. Serialize revocation/cancel against admission/output acceptance. |
| P07 | Storage specialist; compute owner; recovery reviewer | Choose topology and backend only after specifying crash/partition/restore/failover model, fencing enforcement, execution coupling, result/tombstone retention and cleanup. Trace every crash point and require a recovery proof for unknown outcomes. No exactly-once inference claim based on database durability alone. |
| P08 | Dependency-security reviewer; protocol and runtime maintainers | Select exact HPKE artifacts after current release/advisory/license/maintenance/audit review. Record pins, hashes, transitive locks, runtime capability results and same-maintainer assurance limits. No floating latest version or package-name approval. |
| P09 | Independent cryptographic specialist; signing/JCS maintainers | Qualify Ed25519 and strict JSON/JCS implementations independently of HPKE. Record RFC primitive conformance, malformed-input handling, number/Unicode behavior, cross-language byte agreement and independent application-vector review. |
| P10 | All client/compute/relay owners; privacy reviewer | Approve API v1 routing/control allowlist and adapters, safe model/tier selection, pre-relay encryption, every old/new pair and terminal/progress/cancel behavior. Document bounded relay visibility, plaintext-sentinel checks, user-visible unknown execution/failover and fail-closed capability errors. |

Mandatory P03 numbers include maximum request duration and accepted age; maximum
bundle/local-policy, issuer, descriptor and snapshot lifetimes; maximum revocation
freshness and clock uncertainty/skew; maximum strings, IDs, body/option/result
members, nesting, arrays, bundle/chain/snapshot sizes, total plaintext/ciphertext
and transport bytes; progress frequency/count; pending/terminal replay capacity;
retry/result/tombstone retention; key overlap and post-expiry retention. The
candidate fixes some structural maxima, but all operational bounds remain subject
to review. Bound decoded and encoded sizes before allocation and expensive crypto.

For every limit, record unit, allowed interval, enforcement points, rationale,
clock/storage assumptions, overflow behavior, maximum-plus-one rejection and the
interaction with older peers. Local policy may narrow bounds, never silently
relax them. Missing bounds disable the candidate profile. Bound retention must
account for all clocks, late deliveries, recovery and restored backups; eviction
cannot reset replay authority while any relevant replay window is open.

## 3. Library qualification worksheet

Research checked on 11 October 2026 supports a shortlist, not a dependency choice:

| Candidate | Evidence and limit |
| --- | --- |
| [PyHPKE](https://github.com/dajiaji/pyhpke) | Advertises the candidate HPKE primitives/modes. Its project states it has not been formally audited. Qualify exact source/artifacts and Python/runtime integration before selection. |
| [hpke-js](https://github.com/dajiaji/hpke-js) | Advertises the candidate suite and browser support; states it has not been formally audited and limits AEAD sequence numbers to JavaScript's safe integer range. Compare the modular packages with the full package for the exact derivation behavior and target engines. |
| Ed25519 implementation | Unselected. HPKE browser compatibility is not evidence for signing, canonical point handling or key import/export semantics. |
| Strict parser and JCS implementation | Unselected. Must reject duplicates before canonicalization and preserve the candidate numeric/Unicode/binary rules across runtimes. |

PyHPKE and hpke-js share a maintainer account. Neither the package names nor
agreement between those two implementations establishes independent assurance.
Require established RFC primitive vectors, independent expected-output review and
application-composition analysis. Passing any one of those is insufficient alone.
No new package/version is selected, installed or tested by this packet.

Create one qualification row per actual target, with these mandatory columns:

- Client/compute release/source commit; exact supported OS, architecture, runtime
  and browser engine/version; plaintext/encryption boundary and update origin.
- HPKE, Ed25519, strict parser and JCS packages **separately**: exact versions,
  upstream release/source commits, artifact hashes, transitive dependency lock,
  build configuration and trusted delivery chain.
- Maintenance/security policy, current advisories with check date, license,
  available audit scope/date/artifact, unresolved findings and reviewer decision.
- RNG source and failure behavior; key import/export/validation; derivation APIs;
  capability detection; packaging/minification; context/sequence/nonce limits;
  error behavior; secret lifetime/erasure limits; reproducibility evidence.
- RFC/application vector IDs and exact results, independent review record,
  unsupported-capability message and confirmation that no weaker suite activates.

| Required integration surface | Runtime inventory required before qualification |
| --- | --- |
| token.place compute (`server.py`) | Actual supported Python/OS/architecture combinations, operator key boundary, acceptance store and restart/failover deployment topology. |
| Python API client | Supported Python versions, transport adapter, trust policy/clock persistence and packaging/update paths. |
| Desktop client | Each supported OS/architecture and packaged Python/native/webview boundary; installer/update provenance and capability behavior. |
| token.place TypeScript/browser and landing UI | Supported engines/builds and where code originates. A mutable relay-served page cannot qualify as an independent verifier. |
| DSPACE browser client | Each supported browser engine/version, actual dependency/build pins, independent trusted web origin and update path. |
| Gabriel local adapter | Supported Python/OS versions and exact pre-relay encryption placement; local plaintext boundary and delivery provenance. |

These rows are deliberately **unqualified**. Current supported runtime inventories
and actual package pins must be collected from the owning repositories/releases at
G1; README capability claims are not substitutes. A missing supported engine is a
release blocker for that surface, not grounds for automatic compatibility mode.

## 4. Storage and failure-model review

No backend is selected or deployed by this packet.
[SQLite atomic commit](https://sqlite.org/atomiccommit.html) is relevant to a
single-host durable acceptance domain. Its [WAL documentation](https://sqlite.org/wal.html)
describes single-writer operation and same-host shared-memory constraints; WAL is
not a cross-host network-filesystem replication design. Review supported filesystems,
flush settings, disk failure and backup/restore semantics explicitly.

A compute-controlled PostgreSQL service is a shared-domain candidate.
[Serializable isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
can reject unsafe concurrent histories, and applications must retry aborted
transactions correctly. A transaction retry cannot blindly repeat inference.
Unique authority, failover durability, fencing, execution coupling, terminal
persistence and restore safety remain separate proof obligations.

For either candidate, require a transaction/failure diagram covering admission,
capacity reservation, durable worker grant, physical dispatch, completion,
terminal commit and client delivery. Enumerate concurrent retries, cancellation,
revocation, partitions, stale workers, lost acknowledgements, partial updates,
rollback/restore and exhausted capacity. Include a changed request signed by the
same continuity key/nonce after the original expiry, and prove a durable retirement
fence prevents reuse before any tombstone deletion. Show where `outcome_unknown` is retained
and who can provide an independently justified recovery proof. A fresh attempt
must remain visibly separate from earlier possibly executed work.

## 5. Deterministic vector record specification

This section specifies future test artifacts; it provides no key material,
ciphertext, executable generator or claim that vectors have run. G2 needs separate
authorization after the exact schema and parameters are reviewed. Fixtures must be
synthetic, explicitly labeled test-only and unusable for real trust enrollment.
Never include actual operator roots, user prompts, credentials or production data.

Use a versioned vector manifest, one record per case. The candidate record has
exactly these required fields; unknown fields fail fixture validation. Structural
integers use the protocol's `U`; byte strings use its unique base64url form.
This fixture schema must itself be frozen with the protocol at G1.

| Field | Exact fixture content |
| --- | --- |
| `format` | Literal `tp-auth-inference-vectors/1`. |
| `case_id`, `family` | Unique `Id`, family enum `binding`, `parsing`, `replay`, `lifecycle`, `client_privacy`, `compatibility`. |
| `spec_commit`, `profile`, `case_revision` | Exact 40-character lowercase Git SHA; literal candidate profile; positive `U`. |
| `parameters` | Complete approved bound/policy object from P03, plus its canonical bytes/digest. Its exact member schema is a blocking G1 deliverable; no implicit runner defaults. |
| `inputs` | Object with `synthetic_keys`, `random_tape`, `clock_trace`, `policy_before`, `replay_before`, `client_before`, `events`. Each collection has the frozen typed state/event schema from P04-P07; unresolved schema blocks generation. |
| `input_bytes` | Ordered array of `{label:Id,bytes:Bytes}` for every received/sent object; malformed cases preserve original bytes without repair or parse/re-encode. |
| `expected_bytes` | Ordered array of `{label:Id,bytes:Bytes}` for canonical records, framed signing inputs, signatures, descriptor/request digests, HPKE info/AAD, encapsulation and ciphertext. Empty only when failure occurs before any such operation. |
| `expected_decisions` | Ordered array of `{event_index:U,decision:Id,error_class:Id}` using the frozen decision/error registry; `none` for no error. |
| `state_after` | Exact policy/replay/client state using the same frozen schemas as `inputs`; no unspecified state mutations. |
| `permitted_effects`, `forbidden_effects` | Arrays of typed effects with counts/order and sink identity; effect schemas freeze at G1. At minimum cover execution count, worker grant, terminal acceptance, persistence and relay plaintext exposure. |
| `oracle` | Object with `method:Text`, `independent_reviewer:Text`, `reviewed_case_digest:B(32)`, `evidence:Text`. Named, independent acceptance bound to the case; absence means unreviewed. |

An oracle's case digest covers the canonical record **excluding `oracle`**, avoiding
a self-referential hash. Its exact digest label is `vector-case` under the protocol's
framing. Fixture intermediate labels and the key/random/state/event/effect registries
must be enumerated before generation; P01/P04-P09 own their relevant portions.
No free-form runner convention may fill these gaps. Production code must never
expose fixture-only deterministic randomness or secret-intermediate logging.

Record exact synthetic private/public key bytes and deterministic generation
inputs, random-tape consumption order, trusted-time intervals, scheduling order,
initial epochs/fences and crash cut points. The same input must reproduce the same
bytes, decisions and state changes. Record safe test-only intermediate secrets
only when needed for independent HPKE review, never in ordinary application logs.
A peer implementation agreeing with the generator is not the sole expected-output
oracle. Include RFC primitive vectors separately from application vectors.

### Minimum case families and expected safety decisions

| Family | Required positive/negative cases | Required assertions |
| --- | --- | --- |
| Identity and binding | Valid independent root/operator; substituted root/issuer/node/encryption/signing/recipient keys; wrong purpose, domain or model scope; changed descriptor, profile/suite/mode, capabilities, body/options/tier, nonce, route ID, expiry or outer copy. | Valid case authenticates full transcript. Every independent mutation rejects before execution or result use; no root enrollment or downgrade side effect. |
| Authentication and parsing | Forged success/error/progress/cancel; wrong framing/context/direction; bad tag; truncation; malformed/low-order keys; duplicate or escaped-duplicate keys; padding/alternate base64url; unknown fields/suites; BOM/UTF-8/surrogates; Unicode normalization variants; negative-zero, fractional/exponent/overflow integers; decimal-string aliases; every size/depth/delegation boundary. | Reject deterministically at the documented stage; do not canonicalize malformed input into validity. Distinguish canonical byte equality from semantically different Unicode. No plaintext/digest leakage in diagnostics. |
| Replay and failure | Exact retry; changed-content nonce reuse; simultaneous duplicates; rotation/node change in same domain; crash at every admission/grant/dispatch/execution/terminal/delivery boundary; partition/replica disagreement; store loss/restore; capacity exhaustion; stale fence; unknown execution; pre-admission cancellation. | At most one authoritative grant/terminal acceptance; no fresh execution from retry or uncertain recovery; conflicts/tombstones survive restart. Unknown outcomes remain explicit. |
| Lifecycle | Immediately before, exactly at, and after every approved not-before/expiry/freshness/skew boundary; clock rollback/uncertainty; suspend/reboot; missing/stale status; epoch rollback/equal-epoch conflict; cumulative-revocation removal; interrupted update; in-flight revocation; overlap rotation; issuer/root compromise and independent recovery. | Half-open intervals and conservative clock rules hold at all actors. No restoration of authority from old signatures, expired descriptors, stale snapshots or lost state. |
| Client semantics/privacy | One terminal result; repeated/reordered/post-terminal progress; cancellation-success/revocation-acceptance races; local state loss; failover with possible earlier execution; setup cancellation/fingerprint mismatch; mutable relay client code; misplaced Gabriel adapter; missing capability. | No duplicate rendering/tool execution or progress extending expiry. No synthetic plaintext at relay ingress/queue/storage/logs/diagnostics. No claim of independent verification from hostile client delivery. |
| Compatibility | Every pair below; stripped capability advertisement; unsupported profile; explicit saved compatibility versus unset/verified mode; old/new rollback. | Verified mode refuses incompatible peers; compatibility is explicit encrypted API v1 only; no API v2, deprecated routes, TOFU promotion or plaintext fallback. |

For each mutated field, include the original valid case and one isolated change,
then selected combined changes to check rejection ordering. Include concurrency
schedules that reverse the apparent winner of cancellation, revocation and success.
Synthetic plaintext sentinels must be checked at relay boundaries even when an
error aborts early. Rejection must specify forbidden inference and policy effects,
not merely assert an exception was raised.

## 6. All-client interoperability and compatibility matrix

For each required client surface in section 3, instantiate all eight combinations
below using exact old/new releases and relay revisions. `Old` means the existing
encrypted API v1 format. `New` means the qualified authenticated profile and reviewed
adapter; support is not established by this table. Test each distinct packaged
runtime/engine and both request/response directions, plus error/progress/cancel.

| Client | Compute | Relay | Required behavior |
| --- | --- | --- | --- |
| Old | Old | Old | Existing encrypted compatibility behavior; never label verified. |
| Old | Old | New | Encrypted compatibility only if the new relay explicitly retains that format. Otherwise clear refusal. |
| Old | New | Old | Compatibility only if the new compute explicitly supports it; otherwise refusal. No implicit translation into verified status. |
| Old | New | New | Same explicit compatibility limit; a new relay cannot upgrade client trust. |
| New | Old | Old | Verified mode refuses. A separate saved compatibility profile may use the old encrypted path. |
| New | Old | New | Verified mode refuses regardless of relay claims; explicit encrypted compatibility only where supported. |
| New | New | Old | Verified succeeds only if the old relay demonstrably preserves the entire new envelope/controls; otherwise refuses. Never assume opaque forwarding works. |
| New | New | New | Verified succeeds only with enrolled roots, current authority, qualified libraries and all binding/replay/privacy checks. |

An upgraded client with no explicit saved mode stops before all dispatch, including
compatibility. Old Gabriel plaintext-to-relay behavior is **not** an old encrypted
compatibility entry: it must refuse that distributed path until its local encryption
adapter qualifies. Relay-served mutable JavaScript cannot claim verified mode even
in a new/new/new row. Test independent trusted client delivery explicitly.

Also test multiple independently enrolled operators, arbitrary self-hosted relay
URLs, allowed-operator failover, policy-restricted models/tiers and every supported
rotation/rollback release pair. Capture exact byte/vector results and refusals;
passing an honest-relay happy path cannot substitute for malicious-relay cases.

## 7. Gate sequence and evidence records

| Gate | Required evidence and boundary |
| --- | --- |
| G1: protocol review | Named protocol, compute, client, storage/lifecycle/dependency owners and independent cryptographic specialist accept the exact document revision, all P01-P10 decisions, numeric limits and pinned qualification matrix. Every blocking comment resolved or explicitly prevents advancement. |
| G2: separately authorized vectors | Freeze fixture schemas; generate positive/negative vectors with synthetic inputs, reproducibility records and independent expected-output review. Bind to G1 revision and precise library artifacts. |
| G3: separately authorized implementation | Bounded compute, enrollment/persistence and client-adapter changes consume the same accepted contract. Merged docs/vectors do not by themselves authorize implementation. |
| G4: offline interoperability | Every surface/runtime and old/new combination passes shared vectors, negative cases, relay plaintext sentinels, delivery/update checks and lifecycle/recovery tests; record unsupported-target refusals honestly. |
| G5: separately authorized staging and rollout | Synthetic staging only after prior gates; then explicitly approved operator/client rollout with enrollment, trusted delivery, key custody, revocation and independent recovery acceptance. No automatic trust downgrade on rollback. |

Each gate record must name artifact/source commits, scope, reviewers and their
independence/roles, commands/tool versions, outputs/check conclusions, skips and
rationale, limitations, open findings and explicit acceptance date. Any changed
schema/library/runtime invalidates affected evidence until rechecked. An AI review
or green repository CI is useful documentation feedback, not cryptographic signoff.

Enrollment/policy persistence/recovery and Gabriel adapter remediation remain
separate implementation dependencies. This packet completes only a preparation
slice; it cannot close the underlying authentication findings or establish release
readiness. No security setting, trust store, enrollment, credentials, executable
crypto, provider experiment, deployment or release is changed by these documents.
