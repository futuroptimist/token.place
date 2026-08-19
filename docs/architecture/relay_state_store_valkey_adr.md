# ADR: shared Valkey coordination and atomic relay transitions

- **Status:** accepted design; not implemented
- **Date:** 2026-08-19
- **Issue:** [#1569](https://github.com/futuroptimist/token.place/issues/1569)
- **Depends on:** [registration and lease boundary](relay_state_store_registration_lease_adr.md)

## Decision summary

The first shared backend will use Valkey, reached through Sentinel, as the single authority for all
API-v1 coordination state when more than one relay process can serve traffic. The implementation
will use bounded hashes and sorted sets for indexed records, Streams with consumer groups for work
delivery, and small, reviewed Lua scripts for cross-key state-machine transitions. Pub/Sub may wake
pollers, but a subscriber must always reread authoritative durable state.

This ADR finalizes the data model and failure contract needed by the next implementation slices. It
does **not** implement Valkey, connect `relay.py` to `RelayStateStore`, change routes or rate limits,
or alter deployment resources. Current runtime behavior remains process-local, single-process, and
unchanged. In particular, neither this document nor one standalone Valkey pod provides HA.

The earlier registration/lease ADR remains authoritative for its validated capability shape,
credential-digest rule, and memory-backend behavior. This ADR extends that foundation rather than
changing it.

## Current protocol inventory

The current API-v1 path is not a generic queue. Selection filters registrations by API version,
model alias, context tier, health/draining state, and capacity; chooses the smallest capable tier,
then least load, then a round-robin tie break. Enqueue later validates the selected server and adds
a pending deadline. Poll removes the first API-v1 envelope and creates an in-flight lease. Control
renews or reports cancellation/expiry. Response, cancellation, expiry, and unregister contend for
one terminal winner. Retrieval consumes a response or progress update and otherwise distinguishes
pending, terminal, and unknown requests.

### Process-local state to shared authority

| Current state/global | Current purpose | Valkey authority and operation |
|---|---|---|
| `known_servers` | Registration order, capabilities, lease/poll fields, credential digest, and nested in-flight entries | Compute hash plus lease/order sorted sets; claim hashes and lease sorted set. Registration, renewal, claim, unregister, and expiry scripts mutate these records. |
| `server_round_robin_next_index` and `api_v1_filtered_round_robin_next_positions` | Global and eligibility-set tie-break cursors | Cursor hashes keyed by a bounded eligibility-class digest; reserve script advances the selected cursor. |
| `client_inference_requests` and its condition | Per-server encrypted request queues and poll wakeup | One bounded Stream per server, a consumer group, and request records. Stream entries contain only an opaque request-record key and safe routing metadata. |
| `client_pending_request_ids` and `client_pending_request_deadlines` | Request existence, cancellation proof, and authoritative timeout | Pending request hash plus deadline sorted set; store a cancel-token SHA-256 digest, never the token. |
| `api_v1_in_flight_requests` nested in each server | Claim owner, claim expiry, request deadline, and capacity load | Claim hash, claim-lease sorted set, request lifecycle field, and per-server reserved/claimed counters. |
| `client_responses` | Encrypted final envelopes awaiting retrieval | Bounded response hash referenced by the request record, with retrieval state and TTL. |
| `client_progress` | Latest encrypted progress envelope awaiting one retrieval | One bounded replaceable progress hash per active request; progress never extends the request deadline or claim lease. |
| `client_terminal_request_ids` | Cancelled/expired status and reason | Terminal hash with bounded status/reason enum and TTL. |
| `client_terminal_outcomes` | Once-only outcome accounting/deduplication | Outcome field in the terminal/request record; terminal scripts set it with compare-and-set semantics exactly once. |
| `api_v1_control_tombstones` | Owner-visible cancel/expiry control result and acknowledgement | Control tombstone hash keyed by server and request digests, with owner credential digest and TTL; acknowledge deletes it atomically. |
| `api_v1_recently_unregistered_servers` | Distinguish an evicted node from an unknown node | Short-lived unregister tombstone hash with authoritative expiry. |
| Flask-Limiter memory storage and direct `_control_server_owner_identity` lookup | Per-route limits and authenticated control-plane identity | Shared rate-limit keys in a separate bounded sub-prefix; owner lookup reads the compute record through the store. |
| `streaming_sessions`, `streaming_sessions_by_client`, and `stream_lock` | Legacy `/stream/source` and `/stream/retrieve` chunk buffers | Remain legacy process state. API v1 is non-streaming and does not use them. Shared-state mode must reject enabling every legacy relay route, including streaming routes. |

`DRAINING`, locks/conditions, Flask request context, static configuration, injected Secrets,
Prometheus collectors/timers, sockets, and process liveness remain local. Locks may still protect
local caches or client objects, but no local cache may decide a protocol transition.

### API-v1 route to store-operation map

| Route | Atomic store operation(s) |
|---|---|
| `POST /api/v1/relay/servers/register` | `register_or_renew_compute`; returns a raw credential only when the relay generated it, while storing its digest. |
| `POST /api/v1/relay/servers/unregister` | `unregister_compute_and_terminalize`; removes registration/reservations, terminalizes owned work, and creates tombstones. |
| `GET /api/v1/relay/servers/next` | `select_and_reserve`; eligibility inspection and cursor advancement occur in the same transition and return a reservation token. |
| `POST /api/v1/relay/requests` | `enqueue_reserved_request`; validates/consumes the reservation, creates pending state, and appends the Stream entry idempotently. |
| `POST /api/v1/relay/servers/poll` | `claim_next_request`; renews registration/poll liveness, claims one deliverable entry, and converts reserved capacity to claimed capacity. |
| `POST /api/v1/relay/servers/control` | `renew_or_read_control`; renews a live claim, expires it when due, or reads/acknowledges its tombstone. |
| `POST /api/v1/relay/progress` | `replace_encrypted_progress`; validates exact owner and active lifecycle before bounded replacement. |
| `POST /api/v1/relay/responses` | `accept_encrypted_response`; validates ownership, accepts the sole terminal winner, releases capacity, and stores the encrypted response. |
| `POST /api/v1/relay/requests/cancel` | `cancel_request`; validates the proof digest, removes/revokes queued or claimed work, releases capacity, and records terminal/tombstone state. |
| `POST /api/v1/relay/responses/retrieve` | `retrieve_response_or_status`; atomically consumes response or latest progress, or returns pending/terminal/unknown state. |

The relay-served chat completion path and server selection helper must compose
`select_and_reserve` with `enqueue_reserved_request`; they may not reproduce selection against a
local snapshot. Health and the future availability endpoint use side-effect-free `check_schema`
and `inspect_availability`, never a cursor-advancing selection.

## Keyspace and record design

### Namespace and schema gate

The configured prefix is:

```text
tokenplace:{environment}:{cluster}:relay:v{schema}:
```

`environment` and `cluster` use the registration ADR's lowercase routing-safe validation. They
are operator-selected logical names, not addresses. The entire prefix is an ACL boundary. Hash
tags may be added inside the prefix only if a future clustered-Valkey deployment requires them;
Sentinel's single-primary topology does not.

`...:meta` contains `schema_version`, a compatible reader/writer range, and a migration state.
Every process checks it before readiness and before accepting coordination traffic. Schema v1
writers may run together only when their declared read and write ranges overlap the stored
version. Additive fields must have defaults and old writers must preserve unknown fields.
Removing/reinterpreting a field, changing a key or script contract, or changing ciphertext
meaning requires a new schema prefix and a drain-and-cutover migration. An unsupported or
migrating schema fails closed; the relay never initializes an apparently empty incompatible
prefix. Scripts verify the schema version on every write. There is no dual-write compatibility
mode.

### Authoritative records and bounds

All limits are mandatory configuration with conservative finite defaults in the future contract;
the implementation must reject values outside reviewed hard maxima. A record is deleted only by
its transition or authoritative TTL, never by best-effort local cleanup alone.

| Family | Structures and contents | Authoritative expiry/bound |
|---|---|---|
| Compute registration | Hash per node digest: bounded capability allowlist, owner digest, state, registration generation, registered/lease epochs. Lease and registration-order sorted sets. | Lease deadline set from Valkey server time; maximum node count and existing capability bounds from the foundation ADR. |
| Scheduler/reservation | Cursor hash by bounded eligibility digest; reservation hash and expiry sorted set; reserved and claimed counters in compute hash. | Reservation TTL is short and configurable; maximum active reservations per node and globally. Expired reservations decrement counters before new selection. |
| Encrypted queue | Per-node Stream entry references one request digest and includes enqueue epoch/generation only; encrypted envelope is in a bounded request hash. | `MAXLEN` is an approximate safety trim only after admission enforces an exact per-node depth; request deadline and hard lifecycle TTL remain authoritative. |
| Pending request | Request hash contains client/node digests, bounded encrypted request fields, state, enqueue/deadline epochs, proof digest, reservation/claim generation, and response/progress references. Deadline sorted set indexes due requests. | UTC epoch deadline derived from datastore time; global/per-client bounds; lifecycle TTL lasts through terminal retrieval window. |
| Claim/in-flight | Claim hash contains request, server, consumer, claim generation, claimed/lease epochs; lease sorted set and per-node claimed counter. | Short claim TTL capped by request deadline; stale claims are recoverable. |
| Encrypted response/retrieval | Response hash contains only the validated API-v1 encrypted envelope and accepted epoch; request state says available/retrieved. | Size bounded by existing HTTP/envelope limit; response retrieval TTL. Retrieval is destructive/idempotent by lifecycle state. |
| Encrypted progress | Hash contains the latest validated encrypted progress envelope and update epoch. | One value per active request, same envelope bound, expires no later than request lifecycle; cleared by any terminal transition. |
| Control/cancellation | Tombstone hash contains server/client/request digests, owner credential digest, fixed status/reason, deadline and expiry epochs. Unregister tombstone contains node digest and expiry only. | Control and unregister TTLs are configured and hard-capped; acknowledgement may delete a control tombstone early. |
| Terminal/deduplication | Terminal fields contain fixed status (`completed`, `cancelled`, `expired`, or `retrieved`), fixed reason, winner generation, accepted epoch, and outcome-recorded bit. | Terminal dedupe TTL must cover all response, retry, and tombstone windows; bounded total and per-client records. |
| Rate limits | Hashes/sorted sets used by the selected limiter algorithm, keyed by environment/cluster, policy ID, and a one-way bounded identity digest. | Window TTL plus skew margin; finite key count enforced per policy/identity class. No request or ciphertext data. |

Identifiers placed in key names are fixed-length SHA-256 digests of normalized identifiers; the
original public key or request ID is not embedded in a Valkey key. Collision handling compares the
bounded normalized identity stored in encrypted/request metadata only where protocol ownership
requires it; logs and metrics still receive neither value. Raw control and cancellation
credentials never enter Valkey.

### Primitive roles

- **Hashes** hold typed, bounded records and counters. Generic application dictionaries are
  forbidden.
- **Sorted sets** index lease/deadline expiry, registration order, and abandoned reservations.
  Scores are UTC epoch milliseconds obtained from Valkey `TIME` inside scripts.
- **Streams and consumer groups** provide ordered, at-least-once work claims and pending-entry
  recovery. Stream entries are not the lifecycle authority and never carry the full envelope.
- **Transactions/CAS** (`WATCH`/`MULTI`/`EXEC`) are appropriate for uncommon administrative
  compare-and-set changes where bounded retries are safe. Correctness must not depend on a client
  transaction spanning selection and a later HTTP call.
- **Reviewed server-side scripts** implement the cross-key transitions below on the primary. Each
  script has a versioned SHA, a fixed key list/prefix assertion, bounded loops and return shape,
  deterministic behavior, and no dynamic command construction. `EVALSHA` is used after loading;
  `NOSCRIPT` permits one bounded reload and retry.
- **Pub/Sub** may notify waiting pollers or retrieval clients. Lost or duplicate notifications are
  harmless because Streams and hashes are authoritative and every wait has a bounded poll timeout.

## Atomic transition contract

One script invocation is one transition on the current primary. Every transition first verifies
the schema gate, uses datastore time, validates record generations, and returns a fixed result
enum. Cleanup loops process at most a configured batch; callers repeat later rather than allowing
unbounded scripts.

1. **Register or renew compute and lease.** Validate bounds; expire a due prior generation; enforce
   node capacity; create or update capabilities and lease indexes; retain the owner digest on
   renewal; assign a new generation after true re-registration. Registration order changes only
   for a new generation.
2. **Validate eligibility and reserve capacity.** Reclaim a bounded batch of expired reservations
   and claims; evaluate the real model/context/health/draining predicates; choose smallest tier,
   least `(reserved + claimed)` load, then the eligibility cursor; increment reserved capacity;
   create a random, fixed-size reservation token digest tied to node generation, workload class,
   and expiry; and advance both relevant fairness cursors.
3. **Enqueue an idempotent encrypted request.** Use `(client identity digest, request identity
   digest)` as the idempotency key. An identical live retry returns its existing state. A conflicting
   envelope digest is rejected. Validate and consume exactly one live reservation token, convert it
   to queued capacity, create pending/deadline records, and `XADD` one reference. Directly addressed
   requests use the same operation with an internally created-and-consumed capacity reservation;
   they cannot bypass capacity.
4. **Claim queued work and create an in-flight lease.** Authenticate the current node generation,
   reclaim due claims, read the consumer group, and atomically move one queued request to claimed,
   changing queued/reserved accounting to claimed accounting. Record a new claim generation and
   lease capped by the request deadline. Redelivery of the same generation is allowed.
5. **Renew control/in-flight lease.** Authenticate registration and exact claim generation; if the
   request deadline has passed, run expiry instead. Otherwise extend only the claim lease, never the
   request deadline, and return active status. Tombstone reads/acknowledgements are owner-digest
   protected.
6. **Accept encrypted response and finalize terminal state.** Authenticate node and exact active
   claim. If no terminal winner exists, persist the bounded encrypted response, set `completed`,
   clear pending/progress/claim, release capacity, acknowledge/delete the Stream entry, and set the
   outcome bit once. A byte-identical retry returns already accepted; another response or terminal
   winner is rejected as gone.
7. **Cancel, expire, unregister, or evict work.** Validate requester proof for cancellation or node
   ownership/generation for unregister; otherwise require a due server-time index entry. Revoke
   queue/claim/reservation state, release each counter once, and attempt the terminal CAS. Node
   removal applies this in bounded batches and leaves a draining marker until all owned work is
   terminalized; it cannot expose a partially removed schedulable node.
8. **Create the appropriate tombstone.** In the same winning terminal transition, create the
   owner-bound control tombstone for claimed work and the unregister tombstone when applicable.
   Losing transitions do not overwrite it.
9. **Record a terminal outcome once.** The same script that wins terminal state flips an
   `outcome_recorded` bit and returns whether the caller should increment its local metric. A retry
   cannot increment it again. Metrics remain per-process observations; the shared bit is the
   deduplication authority.
10. **Advance the global fairness cursor.** Cursor advancement is part of successful reservation,
    not inspection or enqueue. It stores the successor registration rank and eligibility-class
    cursor. Node removal/re-registration uses stable generation/rank ordering, so stale numeric
    indexes cannot bias or address a replacement node.

### Reservation and fairness decision

A bounded reservation token is required because `/servers/next` and `/requests` are separate HTTP
calls. Selection without reservation races across relay replicas and can exceed `max_concurrency`.
The token is opaque to callers; Valkey stores only its digest. It is single-use, bound to the
selected node generation and normalized workload class, and expires quickly. Enqueue consumes it
atomically. An expired/abandoned reservation is reclaimed from the expiry index and decrements
reserved capacity exactly once. Cursor advancement occurs when reservation succeeds, so abandoned
callers may consume a turn but cannot consume capacity indefinitely. Availability inspection does
not reserve or advance a cursor.

## Delivery, recovery, and consistency

Claims are at least once. After claim-lease expiry, a poller uses consumer-group pending recovery
and a claim-generation CAS to make the work deliverable again. A compute node can therefore receive
the same encrypted request after a relay, connection, or primary failure and must treat request
identity idempotently. Enqueue is idempotent, but duplicate delivery remains possible. Exactly one
terminal transition is accepted; this is not exactly-once execution or delivery.

Valkey primary-to-replica replication is asynchronous. A promoted replica can lack a recently
acknowledged write. Optional `WAIT` after high-value writes may reduce the probability of loss, at
the cost of latency and availability, but cannot make the script plus replication strongly
consistent and cannot provide exactly once. Clients reconcile retries using idempotency keys,
generations, and terminal CAS. A failover that loses accepted state may cause safe redelivery or a
bounded 503/gone result, never acceptance of plaintext or an unbounded retry.

Persisted times are Valkey server UTC epoch milliseconds or validated incoming UTC epoch deadlines.
Python `time.monotonic()` values are never persisted or compared across processes.

## Persistence, memory, and HA operations

Coordination is ephemeral but accepted in-flight work should survive routine restarts. The required
deployment enables AOF with `appendfsync everysec` and periodic RDB snapshots. AOF rewrite and RDB
limits must be sized for the bounded dataset. Backups and long-term history are not required.
Sentinel/operator configuration must prevent an empty restarted node from becoming primary merely
because it is reachable; a node rejoins as a replica, synchronizes from the elected primary, and is
eligible for promotion only after synchronization. Operators must test total-loss recovery as a
new empty coordination epoch that forces compute re-registration and fails old requests closed.

Valkey uses a configured finite `maxmemory` and `maxmemory-policy noeviction`. Admission limits,
TTLs, Stream trimming, and batched cleanup are the primary memory controls. `OOM`/reject-write is a
backend failure: no relay evicts live coordination state or falls back to memory, and the caller
receives a bounded 503. Expiry indexes and record TTLs are reconciled so an orphaned index cannot
make an expired record live.

Production HA discovery uses one primary, replicas, and three Sentinel voters (or a separately
reviewed maintained operator with equivalent election semantics). One standalone Valkey pod is
explicitly not HA. The relay discovers the primary through Sentinel and never writes to a
statically remembered address. On `READONLY`, role change, timeout, reset, or connection failure,
the client discards the connection, rediscovers the primary, reloads scripts if needed, and retries
only operations whose idempotency contract permits it. Connect, socket, Sentinel discovery, and
total request retry budgets are finite; exponential backoff has jitter and is capped by the HTTP
deadline. Non-idempotent-looking calls are safe to retry only with their operation/request token.

The selected Python client is maintained `redis-py` (`redis` package) because it provides Sentinel
discovery, connection pools/reconnect, transactions, scripting, Streams/consumer groups, and
async/sync APIs. The implementation PR must pin a reviewed version and prove Sentinel discovery,
arm64 installation, transaction/script behavior, stale-connection recovery, and compatibility with
a real Valkey test server before adoption. A mock-only or Redis-protocol client without these tests
is not acceptable.

### ACL contract

Credentials come only from an external Secret/configuration channel and are never embedded in
keys, logs, examples, or diagnostics. The relay principal is restricted to its exact
environment/cluster/schema prefix and the minimum command families: connection/health and server
time; hash, sorted-set, key expiry/existence/deletion; Stream/consumer-group operations; script
load/execute; transactions needed by the implementation; and optional publish/subscribe channels
under the same prefix. Administrative commands, keyspace-wide scans, configuration changes,
replication/Sentinel administration, modules, flush, and access outside the prefix are denied.
Scripts are reviewed against that same command allowlist.

## Relay-blind E2EE allowlist

Valkey may contain only validated encrypted API-v1 request, response, and progress envelope fields;
fixed protocol/version fields; opaque identity/request/reservation digests; credential/proof
digests; bounded public routing keys needed by the current protocol; bounded capability/routing
metadata; fixed lifecycle enums; generations/counters; Stream IDs; and UTC epochs/TTLs. Ciphertext
is opaque and size bounded. Public routing keys are validated and bounded record values, never key
names or observability labels.

Valkey must never contain plaintext prompts/messages, model output, tool names/arguments/output,
chat history, decrypted metadata, unvalidated or unbounded routing keys, raw request IDs, raw
control/cancel credentials,
relay private keys, Kubernetes Secrets, URLs/addresses, headers, arbitrary JSON, exception text, or
diagnostic dumps. Registration capability fields remain the narrow allowlist in the foundation ADR.

Errors expose fixed codes and bounded retry hints only. Logs may contain operation name, fixed
reason, duration, status, and irreversible short correlation digest; metrics use fixed low-cardinality
labels and bounded counts. Diagnostics and traces may report schema/backend health and aggregate
depth/age, never keys, values, ciphertext, identifiers, fingerprints, node names, credentials, or
connection endpoints. Tests must inspect all five surfaces plus datastore keys and values.

## Health, rate limits, migration, and rollback

`/livez` stays process-only. `/healthz` performs a bounded backend ping/role and schema check and
returns 503 for unavailable, read-only, or incompatible state, without addresses or credentials.
Zero schedulable nodes does not affect readiness; it belongs to the separate availability endpoint
and metrics described by issue #1569. Store timeouts and pool exhaustion return a fixed,
cache-disabled 503 within the HTTP budget. Graceful shutdown marks only the process draining, stops
new claims/reservations there, and leaves shared accepted work reclaimable.

Rate-limit keys use a separately configurable suffix beneath the same environment/cluster prefix,
for example `...:ratelimit:v1:`. Whenever relay replicas exceed one **or** `RELAY_WORKERS > 1`, both
coordination and rate limiting must be shared. Startup/chart validation rejects memory coordination,
process-local limiter storage, or any silent local fallback in that mode. A backend outage fails
closed with bounded 503 responses rather than granting an independent local allowance.

Staging migration is drain and cut over, never dual write:

1. deploy schema-checking code with shared mode disabled and prove contract tests;
2. provision the reviewed persistent Sentinel topology and ACL;
3. drain API-v1 admissions, allow or explicitly terminalize current memory work, and keep one relay;
4. start that relay against the empty compatible Valkey namespace and let compute nodes re-register;
5. prove one-replica shared-state restart recovery and shared rate limiting;
6. only then use rolling deployment and scale across nodes.

Rollback after cutover keeps Valkey and scales the relay to one replica running the last
schema-compatible build. It must not switch back to process memory and strand accepted work. An
incompatible code rollback requires another drain/cutover, not reading or dual-writing an unknown
schema.

## Required implementation evidence

Before any replica/worker count exceeds one, run the same contract against memory and a real Valkey
server for bounds, TTLs, registration renewal, selection/reservation, queue ordering, wakeups,
claims/reclaim, progress/response retrieval, every cancellation/unregister/expiry race, tombstones,
outcome deduplication, rate limits, and cleanup batches.

Multi-instance tests construct independent relay/store clients against one backend and prove:
register on A, select/reserve and enqueue on B, poll/control on C, respond on A, and retrieve or
cancel on B/C. Concurrency tests cover duplicate IDs/envelopes, reservation exhaustion, simultaneous
claims, response-versus-cancel, unregister-versus-poll/response, deadline and lease expiry, abandoned
reservations, stale generations, and once-only capacity/outcome accounting.

Failure tests cover process termination at every transition boundary, network interruption,
backend restart, primary failover, stale pooled connections after failover, script-cache loss,
asynchronous-replication loss/redelivery, compatible rolling relay versions, and compute automatic
re-registration. Deployment evidence must additionally prove Sentinel quorum and node distribution;
a standalone test server is contract-test infrastructure, not HA evidence.

## Consequences and non-blocking follow-ups

The next bounded slice can extend the store contract with scheduler reservations and idempotent
enqueue, including memory-backend tests, without choosing keys or concurrency semantics again.
Subsequent slices add claims/control, terminal/retrieval transitions, shared rate limits, the real
Valkey backend, health/availability, runtime route migration, and finally deployment changes.

Non-blocking tuning remains for concrete default TTL/count/byte values, `WAIT` policy by operation,
and whether a later Valkey Cluster topology is useful. Those values must be benchmarked within the
hard bounded model above; they may not change the decided state machine, E2EE allowlist, Sentinel
baseline, persistence/noeviction policy, or fail-closed multi-process requirement.
