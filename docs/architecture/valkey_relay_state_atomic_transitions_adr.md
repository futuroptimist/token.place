# ADR: Valkey relay state and atomic transitions

- **Status:** accepted design; not implemented
- **Date:** 2026-08-19
- **Issue:** [#1569](https://github.com/futuroptimist/token.place/issues/1569)
- **Depends on:** [registration and lease boundary](relay_state_store_registration_lease_adr.md)

## Decision scope

This ADR fixes the shared-state data model and transition semantics needed before a Valkey backend
or further `RelayStateStore` behavior is implemented. It does not implement Valkey, connect the
existing store boundary to `relay.py`, change a route, add a dependency, or change a deployment.
The current relay remains single-process, memory-backed, and unchanged. In particular, neither the
current chart's one relay replica nor one standalone Valkey pod is highly available.

The registration ADR remains authoritative for the existing typed registration contract and its
bounds. This ADR extends that foundation; it does not loosen its credential, capability, clock, or
relay-blind rules. API v1 remains the only active, non-streaming relay protocol.

## Current protocol inventory

The design is based on the actual globals and lock-ordered transitions in `relay.py`, rather than a
generic work queue. The following process-local values affect correctness across requests:

| Current state/global | Current use | Proposed authoritative shared state |
|---|---|---|
| `known_servers` (including capabilities, control digest, ping/poll state, and nested `api_v1_in_flight_requests`) | registration, eligibility, ownership, lease, load, poll, control, response, progress, unregister/eviction | node hash and lease sorted set; claim hashes and claim-expiry sorted set |
| `server_round_robin_next_index` and `api_v1_filtered_round_robin_next_positions` | global and filtered-set tie breaking | one cursor hash containing an opaque last-selected node ID per stable eligibility fingerprint |
| `client_inference_requests` and its condition | per-node ordered encrypted work, bounded poll wait | bounded Stream per node and consumer group; Pub/Sub wakeup is optional |
| `client_pending_request_ids` and `client_pending_request_deadlines` | idempotency, retrieval pending state, absolute request timeout | request lifecycle hash plus deadline sorted set |
| `client_responses` | encrypted response retrieval by client/request | response hash plus retrieval-expiry sorted set |
| `client_progress` | replaceable encrypted progress returned while pending | optional bounded progress hash with request deadline TTL |
| `client_terminal_request_ids` and `client_terminal_outcomes` | cancelled/expired status and once-only outcome accounting | terminal hash, terminal-expiry sorted set, and dedup field in the same terminal record |
| `api_v1_control_tombstones` and `api_v1_recently_unregistered_servers` | tell an owner to stop; distinguish an unregistered owner | control and node tombstone hashes with expiry sorted sets |
| Flask-Limiter storage and the control-plane `FixedWindowRateLimiter` (`memory://` fallback) | public and control-plane quotas, including owner lookup | namespaced shared rate-limit keys using the same Valkey service |
| `streaming_sessions` and `streaming_sessions_by_client` | legacy streaming helpers only | no shared record; legacy routes are rejected in shared-state HA mode |

Locks, conditions, per-process Prometheus objects and HTTP timers are implementation mechanisms,
not shared records. Process liveness/draining, static configuration, injected Secrets, datastore
connections, and relay private keys also stay local. Aggregate metrics may be emitted independently
by each process; no metric registry is coordination state.

### Active API-v1 route mapping

| Route | Atomic store operation(s) |
|---|---|
| `GET /api/v1/relay/servers/next` | `select_and_reserve`; accepts the request identity generated before selection and returns the selected public key and an opaque reservation token |
| `POST /api/v1/relay/servers/register` | `register_or_renew_node` |
| `POST /api/v1/relay/servers/unregister` | `unregister_node_and_transition_work` |
| `POST /api/v1/relay/servers/poll` | `claim_next_request`, followed by bounded blocking/retry when empty |
| `POST /api/v1/relay/servers/control` | `renew_claim_or_read_control` (and acknowledgement of a tombstone) |
| `POST /api/v1/relay/requests` | `consume_reservation_and_enqueue` |
| `POST /api/v1/relay/requests/cancel` | `cancel_or_expire_request` |
| `POST /api/v1/relay/responses` | `accept_response_and_finish` |
| `POST /api/v1/relay/progress` | `replace_encrypted_progress_if_claimed` |
| `POST /api/v1/relay/responses/retrieve` | `retrieve_or_ack_response` or `read_pending_or_terminal`; an acknowledgement token confirms a prior delivery, while an unacknowledged encrypted response remains replayable |

Registration validation and fixed-schema HTTP validation remain outside the store. Owner-authenticated
rate-limit identity lookup becomes a bounded store lookup rather than direct `known_servers` access.
The future availability endpoint calls a side-effect-free `inspect_eligibility`, never selection or
reservation.

## Key space, schema, and bounded records

### Prefix and compatibility

Every key begins with the configured, routing-safe prefix:

```text
tokenplace:{<environment>:<cluster>}:relay:v<schema_version>:
```

`environment` and `cluster` use the namespace validation established by the registration ADR and
must not contain credentials or addresses. The rendered pair inside the single brace pair (for
example, `{staging:relay-a}`) is the one Redis Cluster hash tag; the angle-bracketed names above are
template placeholders, not additional braces. This permits multi-key scripts while retaining an
explicit environment/cluster boundary. IDs placed in suffixes
are fixed-length SHA-256 digests of canonical identifiers. Raw public keys and request IDs are
record fields only where the API must return them; they are never key names.

`...:schema` is a non-expiring manifest containing the schema major, active schema revision, active
writer revision, minimum/maximum supported reader revisions, minimum/maximum supported writer
revisions, script digests, and a migration epoch. Each relay declares its own reader and writer
revision. Before readiness and before every reconnect, it verifies that the manifest major matches,
that its reader revision is inside the manifest's supported reader range, that the active schema
revision is inside the relay's supported schema-read range, and that its writer revision and the
manifest's active writer revision are both inside the manifest's supported writer range and the
relay's supported writer range. A read-only health probe must pass the reader checks; any operation
that can mutate must pass both reader and writer checks immediately before dispatch.

Additive fields must have defaults understood by every supported reader, and supported writers must
preserve unknown fields. Removing/renaming fields, changing their meaning, key layout, script
contract, or encoding requires a new major prefix. A relay with an incompatible reader or writer
returns bounded HTTP 503 `state_schema_incompatible`, performs no read that could be interpreted as
protocol state, and performs no mutation. A rolling release may span only the explicitly declared
reader and writer intersections; operators update the manifest ranges/active revisions only through
a reviewed compatibility transition proven against every version in those ranges. There is no
opportunistic cross-major read, dual-write, or lazy conversion.

### Families and limits

All JSON-like fields use a canonical, length-checked encoding before a transaction. Hashes are
preferred for bounded mutable records; sorted sets are authoritative indexes for deadlines and
eligibility scans; Streams provide ordered queue entries and consumer-group recovery. Limits below
are configuration values with repository-defined safe maxima in the future contract, not unbounded
operator knobs.

| Family and keys (after the prefix) | Record and authority | Bound and authoritative expiry |
|---|---|---|
| `node:{node_digest}`, `nodes:lease` | registration time, API-v1 capabilities, public key, owner credential digest, state, lease epoch | registration ADR bounds; at most configured node capacity; lease zset score uses server-derived UTC epoch and expiry deletes the hash |
| `cursor` | last-selected node digest for global and bounded eligibility fingerprints | at most configured scheduler-fingerprint count; inactive entries expire after terminal retention |
| `reservation:{token_digest}`, `reservations:expiry` | random 256-bit opaque token digest, node/client/request digests, requested model/tier, deadline | one per admitted request, capped globally and per client/node; short reservation TTL, no longer than request deadline |
| `queue:{node_digest}` plus consumer group | exact validated API-v1 encrypted request envelope and safe routing/deadline metadata | per-node configured depth and envelope-byte limit; approximate `MAXLEN` is forbidden for live work; enqueue rejects at the hard bound; entry is deleted only by a terminal/requeue transition |
| `request:{client_digest}:{request_digest}`, `requests:deadline` | canonical identity, state (`reserved`, `queued`, `claimed`, `response_ready`, or terminal), node, queue entry, claim generation, cancellation-token digest, UTC deadline | global/per-client request cap; request deadline is authoritative; lifecycle retained through response/terminal TTL |
| `claim:{client_digest}:{request_digest}`, `claims:expiry` | canonical client/request identity, node and owner digest, consumer/claim generation, lease epoch; expiry members contain both identity digests | one per canonical client/request pair; claim lease TTL bounded by absolute request deadline; expired claims are reclaimable |
| `response:{client_digest}:{request_digest}`, `responses:expiry` | exact validated encrypted response envelope, accepted epoch, retrieval acknowledgement-token digest, acknowledgement state, and replay deadline | one bounded envelope per request; unacknowledged responses remain idempotently replayable until the lesser of the configured response-retention deadline and total lifecycle maximum; acknowledgement deletes or marks the envelope consumed atomically |
| `progress:{client_digest}:{request_digest}` | latest exact validated encrypted progress envelope | one bounded envelope, replacement only; expires no later than the request |
| `control:{node_digest}:{client_digest}:{request_digest}`, `control:expiry` | fixed terminal status/reason, canonical client/request identity, owner digest, acknowledgement state; expiry members contain the node and both identity digests | one per affected claim; configured tombstone TTL capped at five minutes |
| `node_tombstone:{node_digest}`, `node_tombstones:expiry` | unregistered/expired marker and owner digest | at most recent-node capacity; five-minute maximum TTL |
| `terminal:{client_digest}:{request_digest}`, `terminals:expiry` | one fixed outcome/status/reason, accepted response digest when applicable, outcome-counted flag | one per request; configured terminal TTL; the record is the dedup authority |
| `ratelimit:{route_class}:{identity_digest}:{window}` | fixed-window counter and window epoch | bounded route classes and digest identities; expires at window end plus clock-skew allowance |

Expiry indexes are necessary because key TTL expiry cannot itself perform related multi-key cleanup.
Each transition first reaps a bounded number of due members using datastore time, and a bounded
background sweeper invokes the same reviewed transition scripts. Key TTLs are a final memory guard;
zset deadlines and transition logic are the protocol authority. No persisted value uses
`time.monotonic()`. Scripts obtain Valkey `TIME`, and clients express externally supplied deadlines
as validated UTC epoch values.

## Atomicity and data-structure roles

The public store API exposes state-machine operations, never arbitrary key/value access. A single-key
transaction/CAS (`WATCH`/`MULTI`/`EXEC`) is appropriate for schema creation and simple registration
updates. The protocol operations below touch hashes, zsets and Streams together and therefore use
small, versioned Lua scripts loaded by SHA. Scripts are source-controlled, reviewed, deterministic,
bounded in keys/iterations/output, declare every key, call no unbounded scans, and return fixed result
codes. The client handles `NOSCRIPT` by loading only the expected source and retrying once. Script
digests are part of the schema compatibility manifest.

Pub/Sub may wake a long-polling relay after enqueue, response, control, or registration change. A
lost, duplicated, or reordered notification has no correctness effect: consumers always re-read the
Stream/hash and use bounded polling as fallback. Pub/Sub is never an acknowledgement, queue, lease,
health source, or terminal-state authority.

### The ten required transitions

1. **Register or renew.** Validate bounds, reap the addressed expired registration, compare the
   credential digest for a live node, upsert its node hash, and update `nodes:lease` using server
   time. Registration retains the original owner digest as specified by the registration ADR.
2. **Validate eligibility and reserve capacity.** Reap bounded expired leases/reservations/claims;
   apply compatible model, sufficient context, healthy/non-draining, smallest capable tier, least
   load, round-robin tie break, and concurrency limit; count queued + claimed + unconsumed
   reservations; create a reservation and advance the applicable cursor in the same script.
3. **Enqueue idempotently.** Match client/request identity and reservation token, deadline, selected
   node, and envelope bounds. If already queued/claimed/completed, return the existing safe result.
   Otherwise check the hard Stream depth, append exactly once, create/update lifecycle state and
   deadline index, then consume the reservation atomically.
4. **Claim.** Read the next eligible Stream entry, reject terminal/expired work, authenticate the
   node owner, assign a monotonically increasing claim generation, create the claim lease and zset
   member, and associate the Stream pending entry. Only one claimant wins a generation.
5. **Renew control/in-flight lease.** Authenticate the node owner and exact claim generation, reject
   terminal or deadline-expired work, and extend the claim no later than its request deadline. If a
   tombstone exists, return its fixed control state instead of renewing; acknowledgement is atomic.
6. **Accept response and finalize.** Authenticate owner and generation; win a CAS from active to
   `response_ready`; store one bounded encrypted response; remove claim/queue/progress state; create
   the terminal/dedup record with `completed`; and increment the outcome only once. Competing
   response, cancellation, expiry, or unregister receives the existing terminal result. Immediately
   before acceptance, the reviewed non-mutating `server_time_v1` transition samples Valkey's exact
   `(seconds, microseconds)` time. The relay canonically converts it to the in-memory contract's epoch
   float and locally derives the HMAC-SHA-256 acknowledgement token from the shared injected key,
   canonical raw identity digests, network-order IEEE-754 epoch double, and response digest. Only
   SHA-256 of the raw token's lowercase hexadecimal ASCII representation is passed to and persisted
   by `accept_encrypted_response_v1`, together with the canonical epoch; the key, raw token, and
   key-equivalent HMAC state never reach Valkey. That single mutating script calls `TIME` again,
   rejects a malformed or future preflight epoch, revalidates both inclusive deadlines against its
   own time, and atomically persists response, acknowledgement digest, and terminal state. There is
   no provisional token field, post-acceptance digest write, lazy initialization, or ambiguous
   mutation retry. An operation-level exact retry reads the retained terminal authority and returns
   its original generation, acceptance epoch, and replay epoch without recording a new outcome.
7. **Cancel, expire, unregister, or evict.** Win the same lifecycle CAS, remove reservation/queue/
   claim/progress state as applicable, release capacity, and apply a fixed terminal status/reason.
   Unregister/evict processes only a bounded batch and records continuation work; the node becomes
   immediately ineligible before batches run.
8. **Create tombstone.** As part of transition 7, create the owner-bound control tombstone and expiry
   index when a compute owner might still be working. Never create an unauthenticated free-standing
   tombstone.
9. **Record terminal outcome once.** Create the terminal hash with `SETNX`/CAS semantics or observe
   the existing hash; set `outcome-counted` in the same script. Metrics consume the returned
   `new_outcome` boolean, so retries cannot double-count application outcomes.
10. **Advance fairness cursor.** Selection stores the selected node as the next scan anchor (not a
    fragile numeric list index) in the same atomic script that creates its reservation. Reclaiming an
    unused reservation releases capacity but does not rewind the cursor, preventing crash/retry bias.

### Failure-safe response retrieval

Retrieval is a two-phase, bounded protocol because Valkey cannot know whether an HTTP response
reached its client. An authenticated retrieval without an acknowledgement returns the exact stored
encrypted response and its opaque acknowledgement token but does not consume or delete the record.
Any relay can reproduce the token without storing it, and repeating that retrieval returns the same
response and token until its replay deadline. A later authenticated retrieval call may echo that
token as an acknowledgement; the store compares its digest and canonical client/request identity,
then atomically marks the response acknowledged and
removes the envelope and retrieval-expiry member. Duplicate acknowledgements return the same safe
consumed result. A missing, wrong, expired, or cross-request token never consumes a response.

A connection failure before the client receives the first response or its acknowledgement result is
therefore recoverable by retry. The replay deadline remains authoritative even after one or more
reads: reads do not shorten or refresh it. Once that bounded deadline expires, the reaper atomically
removes the encrypted envelope and records the fixed terminal retrieval-expired result; expiry is
allowed to make an unacknowledged response unavailable. Raw acknowledgement tokens are never stored,
logged, included in keys, or exposed to a different canonical identity.

### Selection, enqueue, and abandoned reservations

The current `/servers/next` selection advances cursors but does not reserve capacity; a later
`/requests` enqueue can race another relay. Shared-state mode therefore requires the client to create
its opaque request ID **before** selection. Every `/servers/next` call supplies the bounded client
public key and request ID in addition to model and context tier; the relay derives the canonical
`(client_digest, request_digest)` identity rather than trusting client-supplied digests. Selection
creates a bounded, single-use reservation token for that identity and returns the token alongside the
selected public key. Because only the token digest is persisted, an identical selection retry returns
the unconsumed reservation metadata and selected node but cannot return or reconstruct the raw token.
The client must retain the token returned by the creating call; if that response is lost, it waits for
the short reservation expiry, after which selection may create a fresh token for the still-unqueued
identity. Conflicting parameters fail with a fixed error. This deliberately resolves the otherwise
contradictory requirements to return an existing raw token on retry while never persisting raw token
material or a secret capable of reconstructing it.

`/requests` must echo the token and the same identity, and enqueue consumes it only after all fields
match. A client may cache node metadata for display or encryption, but it must obtain a fresh
identity-bound reservation for every new request; cached-node reuse cannot bypass `/servers/next` or
reuse another request's token. Adding these selection parameters and response/request token fields is
compatible with the wire schema, but shared-state multi-worker mode fails closed for clients that
omit them; legacy clients remain supported only in single-process memory mode until upgraded. Raw
selection identity values are validated and bounded. Reservations store their canonical digests;
queued lifecycle records retain the exact public key and request ID as the minimum routing metadata
needed to deliver and correlate ciphertext. They do not accept arbitrary payload fields. The token
is never logged, and only its digest is stored. Enqueue retains that consumed digest in private
idempotency state for the queued lifecycle, so only the original token with the identical canonical
request can repeat successfully; cleanup removes the digest with its queued record.

Reservations count toward node concurrency and queue bounds. Enqueue consumes one; cancellation,
deadline expiry, or the short reservation TTL releases it through the same bounded reaper. A retry
with the same client/request identity returns the existing reservation or lifecycle result and never
adds load twice. Cursor advancement occurs at reservation time; expired reservation cleanup does not
select a replacement silently.

## Delivery and failover guarantees

Claims are **at least once**. An expired consumer-group pending entry may be claimed by another poller
with a new generation. A former owner can therefore finish computation after failover, but its stale
generation cannot renew or commit. Duplicate delivery and duplicate computation remain possible.
Enqueue is idempotent for the canonical client/request identity, while exactly one terminal CAS and
one encrypted response can be accepted. This is not exactly-once delivery.

Valkey primary-to-replica replication is asynchronous. A promoted replica can lack acknowledged
writes. `WAIT` may optionally be used after important writes to reduce the probability of loss, with
a bounded timeout and an explicit degraded result, but it neither makes failover strongly consistent
nor produces exactly-once semantics. Clients and compute nodes must safely retry, tolerate reclaim,
and re-register after loss. Fencing by claim generation prevents a known stale owner from committing
after a newer claim in the surviving history; it cannot recover a transition absent from that history.

## Availability, persistence, and clients

### Persistence and memory policy

The HA deployment contract is one primary, replicas, and three Sentinel voters (or a separately
reviewed maintained HA operator), distributed across staging nodes. A standalone pod must never be
described as HA. Enable AOF with `appendfsync everysec` **and** periodic RDB snapshots. Long-term
backups are unnecessary, but routine restarts must not intentionally discard accepted live work.
Startup and Sentinel promotion must use persisted datasets and replication lineage; automation must
never treat an empty restarted primary as authoritative and cause replicas with useful state to
resynchronize from it. Recovery procedures stop or fence an empty former primary before rejoining it
as a replica.

Set an explicit memory limit and `maxmemory-policy noeviction`. Live coordination records must never
be silently evicted. Capacity checks reject admission before configured logical bounds; if Valkey
still returns OOM/reject-write, the mutation fails closed with a fixed 503 and existing records remain
authoritative. Cleanup is bounded and monitored; raising memory is not a substitute for record caps.

### Discovery and retry

Use the maintained `redis` Python package (redis-py), through its Sentinel client and master discovery,
not a bespoke protocol client. The implementation PR will pin a supported release only after its
test matrix verifies Sentinel discovery, Linux arm64 wheels/source installation, transactions,
script loading, reconnect behavior, connection pools, TLS/auth configuration, and compatibility with
a real Valkey test server. Those are release gates, not deferred architectural choices.

Clients use Sentinel service discovery, discard connections after `READONLY`, role change, socket
failure, or connection reset, rediscover the primary, and retry only operations carrying an
idempotency identity. Connect, socket, command, and total retry budgets are finite; use capped
exponential backoff with jitter and no unbounded library retry. Ambiguous mutation results are
resolved by reading lifecycle state before retry. Non-idempotent-looking public methods are scripts
whose identity/token makes retry safe.

`/livez` remains process-only. `/healthz` checks a bounded ping, writable-primary role, schema
manifest, and expected script/schema compatibility and returns 503 on failure; absence of compute
capacity does not affect readiness. Store failures and schema mismatch produce bounded 503 bodies
with fixed codes such as `state_backend_unavailable` or `state_schema_incompatible`, and no endpoint,
key, address, exception string, or credential. Graceful shutdown stops new reservations/claims while
leaving shared accepted work reclaimable.

### ACL and secrets

Connection information and credentials come only from injected Secrets; no credential is embedded in
the prefix, chart values, logs, or this ADR. The relay ACL is restricted to the configured prefix and
the minimum reviewed commands: connection/health and role discovery (`PING`, `ROLE`), hash/string
reads and writes, expiry (`HGET`, `HMGET`, `HSET`, `GET`, `SET`, `DEL`, `EXPIRE`, `PEXPIRE`, `TTL`,
`PTTL`), sorted sets (`ZADD`, `ZREM`, `ZRANGE`, `ZRANGEBYSCORE`, `ZCARD`), Streams/groups (`XADD`,
`XREADGROUP`, `XACK`, `XDEL`, `XPENDING`, `XAUTOCLAIM`, `XGROUP`), transactions (`WATCH`, `MULTI`,
`EXEC`, `UNWATCH`), scripts (`EVALSHA`, narrowly controlled `SCRIPT LOAD/EXISTS`), server time
(`TIME`), and optional wakeups (`PUBLISH`, `SUBSCRIBE`). Administrative, global scan, flush, config,
module, debug, and arbitrary script commands are denied. Sentinel uses a separate least-privilege
discovery credential/ACL where authentication is supported; data credentials are not reused as
operator credentials.

## Relay-blind E2EE allowlist

Valkey may contain only validated API-v1 ciphertext envelope fields (`protocol`, `version`,
`ciphertext`, `cipherkey`, `iv`) and bounded routing metadata: public keys only where protocol return
requires them; fixed-length identity/credential/cancellation digests; model IDs and bounded compute
capabilities; fixed lifecycle states/reasons; claim generations; bounded counts; schema/script
versions; and UTC epoch timestamps/deadlines. Raw control or cancellation credentials, relay private
keys, registration tokens, connection secrets, prompts, messages, rendered templates, tool calls or
arguments, tool output, plaintext model output, decrypted content, arbitrary headers, request bodies,
URLs, exception text, and tracing baggage are forbidden.

Keys contain only the fixed prefix and digests. Errors use fixed codes and counts. Logs, metrics,
diagnostics, and traces may expose only fixed operation/reason labels, bounded aggregate counts and
latency; they must not contain raw keys, public keys, node names, request IDs, credentials, URLs, or
either plaintext or ciphertext payloads. Datastore-command logging and tracing values are disabled.
Tests inspect all of these surfaces. Any envelope that fails the allowlist or size check is rejected
before storage; there is no plaintext fallback.

`streaming_sessions` and `streaming_sessions_by_client` support only legacy behavior; API v1 is
non-streaming and does not use them. They will not be migrated. Shared-state HA configuration must
reject enabled legacy relay routes at startup/chart validation rather than leave their ownership
process-local.

## Shared rate limits

Both Flask-Limiter and the control-plane limiter use the same shared Valkey service with a distinct,
environment/cluster/schema-prefixed `ratelimit:` subtree and bounded hashed identities. Control owner
identity is obtained through the store boundary. When relay replicas exceed one **or**
`RELAY_WORKERS > 1`, configuration must require Valkey for coordination and rate limiting; startup
and chart validation reject memory storage. Runtime outage fails closed for limited routes. There is
no process-local fallback after startup or during an outage.

## Migration, rollback, and verification gates

Staging migration is a drain-and-cutover, never dual-write:

1. implement the expanded memory contract and route adapters while retaining one process;
2. implement the Valkey backend and run the identical contract against a real Valkey server;
3. configure the reviewed persistent Sentinel topology and shared rate limits;
4. drain accepted work, stop the memory-backed relay, start **one** Valkey-backed relay, and let
   compute nodes re-register;
5. prove restart recovery, then scale to node-spread replicas only after all gates below pass.

Rollback after cutover scales to one relay replica while **retaining Valkey**. Reverting to process
memory would strand accepted work and is prohibited. An incompatible schema release instead drains,
creates a new major prefix while stopped, and cuts over once; it does not dual-write.

Before replicas scale above one, the memory and real-Valkey contract suites must cover TTL/expiry,
renewal, scheduler choice/reservation, queue ordering and wakeup loss, claim/reclaim, retrieval,
cancellation/unregister/expiry races, tombstones, terminal deduplication, bounds, and server-time
deadlines. Independently constructed relay instances sharing one store must prove register on A,
select/enqueue on B, poll/control on C, response on A, and retrieve/cancel on B or C.

Retrieval contract cases must simulate disconnects before and after the response bytes are written,
prove retry returns the identical encrypted envelope and acknowledgement token, prove only the
matching identity/token consumes it, prove duplicate acknowledgement is idempotent, and prove an
unacknowledged response remains replayable until (but not after) its fixed replay deadline. Rolling
compatibility cases must exercise every supported old/new relay pairing as both reader and writer,
including additive unknown-field preservation, and prove out-of-range readers and writers remain
unready and fail closed without mutation. Reservation plus idempotent enqueue is the next
implementation slice; these contract gates precede runtime or HA rollout.

Concurrency/failure tests must cover duplicate identities, simultaneous claim, capacity reservation,
response-versus-cancel, unregister-versus-poll, lease/deadline expiry, pod termination, store network
interruption, primary failover, stale connections, backend restart, compatible rolling versions, and
compute re-registration. Failover assertions must acknowledge possible acknowledged-write loss and
duplicate delivery while still proving fencing and exactly one accepted terminal transition in the
surviving history. E2EE tests inspect keys, values, errors, logs, metrics, diagnostics, and traces.

## Consequences and non-blocking follow-ups

This design adds a mandatory selection reservation in shared mode and accepts at-least-once work in
exchange for safe horizontal coordination. Streams do not replace lifecycle hashes, and TTL does not
replace explicit terminal transitions. Persistence reduces routine restart loss but does not turn
ephemeral relay coordination into durable chat history.

Non-blocking follow-ups may tune reviewed numeric defaults from load tests, decide whether optional
Pub/Sub wakeups improve latency enough to enable, and evaluate an HA operator instead of Sentinel.
They may not change the prefix/schema rules, reservation requirement, terminal CAS, relay-blind
allowlist, no-eviction/fail-closed behavior, persistence baseline, or migration/rollback model without
a superseding ADR.
