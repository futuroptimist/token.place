# ADR: Valkey state model and atomic relay transitions

- **Status:** accepted design; not implemented
- **Date:** 2026-08-19
- **Issue:** [#1569](https://github.com/futuroptimist/token.place/issues/1569)
- **Builds on:** [registration and lease boundary](relay_state_store_registration_lease_adr.md)

## Context and scope

The registration ADR established the typed, transition-oriented `RelayStateStore` boundary and
its memory implementation. This ADR makes the remaining blocking decisions for a Valkey backend.
It does not change that contract, connect it to `relay.py`, install a client, or change runtime or
Kubernetes behavior. The relay remains process-local, single-process software after this change;
horizontal scaling and HA are **not** implemented.

This is an ephemeral distributed-coordination design, not chat history or a business system of
record. One standalone Valkey pod is not HA. An HA deployment requires a primary, replicas, and
three independent Sentinel voters (or a separately reviewed maintained operator) distributed
across failure domains.

### Current implementation inventory

The design below follows the active API-v1 implementation rather than inventing a generic queue.
Today `known_servers` contains registration, capabilities, heartbeat/polling state, control-owner
digest, and nested in-flight requests. `server_round_robin_next_index` and
`api_v1_filtered_round_robin_next_positions` hold fairness cursors. Per-server lists in
`client_inference_requests` hold encrypted work and a condition variable wakes long polls.
`client_pending_request_ids` and `client_pending_request_deadlines` track accepted identities,
cancel proof and deadlines. `client_responses`, `client_progress`, terminal IDs/outcomes, control
tombstones, and recently-unregistered markers complete the API-v1 lifecycle. Locks make compound
operations atomic only inside one process.

The legacy-only `streaming_sessions` and `streaming_sessions_by_client` belong to `/stream/*` and
deprecated legacy paths. API v1 is non-streaming and does not use them. They will remain local for
single-process legacy compatibility; shared-state mode MUST reject startup when legacy routes are
enabled. They must not be migrated or used as an API-v1 fallback.

The following is the authoritative mapping. “Hash” means a bounded Valkey hash record; “ZSET” is a
sorted-set expiry/index; “Stream” is a bounded encrypted-work log.

| Current state/global | Authoritative shared representation | Operation |
|---|---|---|
| `known_servers` registration/capabilities and owner digest | node Hash + node-lease ZSET | register/renew, unregister/evict |
| `server_round_robin_next_index`, filtered positions | scheduler Hash containing one generation/cursor | reserve-and-advance |
| queue lists in `client_inference_requests` | one Stream per node, one consumer group | enqueue, claim/reclaim |
| implicit queue depth and nested in-flight count | node capacity Hash counters + bounded reservation Hashes/ZSET | reserve, consume/release reservation |
| `client_pending_request_ids` and deadlines | request Hash + request-deadline ZSET | enqueue, claim, terminalize |
| nested `api_v1_in_flight_requests` | claim Hash + claim-lease ZSET; Stream pending-entry list is delivery evidence | claim/renew/reclaim |
| `client_progress` | one replaceable progress Hash per request | accept progress, retrieve/clear |
| `client_responses` | response Hash + response-expiry ZSET | accept response, retrieve |
| terminal request IDs and terminal outcomes | terminal Hash, outcome marker, terminal-expiry ZSET | terminalize once |
| `api_v1_control_tombstones` | tombstone Hash + tombstone-expiry ZSET | terminalize, acknowledge |
| recently-unregistered servers | generation/retirement field in node/tombstone records | unregister/evict |
| Flask-Limiter/`limits` process storage and owner lookup | separately prefixed shared rate-limit keys; owner lookup via node record | rate-limit increment |
| stream session maps | local legacy-only state, forbidden in shared mode | no shared operation |

Process liveness and `DRAINING`, locks/conditions, request contexts, static configuration,
Prometheus registries, HTTP timers, connections, and caches are local. They may be reconstructed
or lost without changing protocol truth. Registration, selection/capacity, work lifecycle,
responses, terminal decisions, and rate limits are shared correctness state.

## Key space, schema, and bounds

Every key begins `tp:{environment}:{cluster}:relay:v1:`. Environment and cluster are validated,
lowercase routing-safe configuration values; braces make the namespace a single cluster hash tag.
Deployments MUST NOT share the pair. The schema marker is
`tp:{environment}:{cluster}:relay:v1:schema`, with `{major: 1, min_reader, max_reader}`. Rate limits
use `tp:{environment}:{cluster}:ratelimit:v1:` and cannot collide with lifecycle keys.

Key suffixes contain HMAC-SHA-256 or SHA-256 digests of bounded protocol identities, never raw
credentials, raw public keys, or request IDs. Collision handling fails closed rather than
overwriting a record whose stored identity digest does not match. Records use fixed field
allowlists and byte/count limits enforced before a write. Configuration supplies concrete maxima
for nodes, queued and live requests per node and namespace, responses, tombstones, progress
records, record/envelope bytes, and TTLs. Reaching a bound rejects the new write with a bounded
capacity response; it never evicts live coordination state.

The contract baseline is 1,024 registered nodes; 1,024 queued requests per node and 65,536 per
namespace; 128 live claims per node (further limited by advertised concurrency); 65,536 live
requests, responses, progress records, reservations, and tombstones per namespace; 64 KiB for an
identity/public-key field; and 1 MiB for each validated encrypted envelope. Reservation TTL is 15
seconds, registration and claim leases default to 30 seconds, request deadlines are 1--3,600
seconds, response retention is 3,600 seconds after completion, tombstone retention is 300 seconds,
and terminal/deduplication retention is 3,900 seconds. Deployments may lower admission/byte bounds
or TTLs, but may not raise them without a contract and memory-capacity review; the advertised node
concurrency ceiling remains 128. Expiry cleanup grace is 60 seconds. These defaults preserve the
current API-v1 lease, request-deadline, tombstone, capability, and identity ceilings.

Authoritative time is Valkey server `TIME` inside reviewed scripts. Persisted deadlines are UTC
epoch milliseconds and sorted-set scores; Python `time.monotonic()` values never enter shared
state. Key TTLs are cleanup backstops, while a record deadline is the transition authority:

| Family | Record and authoritative lifetime |
|---|---|
| Compute registration/capabilities | bounded Hash; lease deadline from registration TTL; lease ZSET; key TTL = deadline + cleanup grace |
| Scheduler cursor | singleton bounded Hash, no idle TTL; schema/namespace lifetime |
| Capacity reservation | token Hash with selected node, request identity digest, generation and expiry; reservation ZSET; short configured TTL |
| Encrypted queue | per-node bounded Stream, trimmed only after terminal/ack plus retention grace; request deadline remains authoritative |
| Pending request | request Hash with state, node, identity/cancel-token digests, enqueue time and deadline; deadline ZSET; TTL through terminal retention |
| Claim/in-flight | claim Hash with node generation, Stream entry, delivery attempt and lease deadline; claim ZSET; key TTL = deadline + reclaim grace |
| Encrypted response | one bounded Hash per request; retrieval state and expiry; response ZSET; TTL through response retention |
| Encrypted progress | one bounded replaceable Hash per active request; expires no later than request/claim deadline |
| Control/cancellation tombstone | bounded status, reason-code allowlist, owner digest, request deadline and expiry; tombstone ZSET; configured short TTL |
| Terminal/deduplication | immutable winning status/reason/epoch plus outcome-once marker; terminal ZSET; TTL exceeds maximum retry/response retention |
| Shared rate limit | counter/window keys owned by the selected limiter implementation; TTL exactly the bounded window plus clock-skew grace |

The queue Stream is capped by configured admission counters, not approximate trimming of live
entries. Cleanup may `XDEL`/trim only entries whose request is terminal and past retention. All
secondary indexes are repairable from authoritative records, but repair is bounded per run.

## Rolling compatibility

Schema major 1 has additive fields with defaults. Each relay declares a supported reader range and
writer version. On startup and periodically, it reads the marker and performs a scripted compare
before writing. Mixed binaries may roll only when their reader ranges overlap and the active writer
version remains readable by every replica. Writers do not emit a new optional field until the
marker advertises the compatible minimum reader. Unknown fields are ignored but preserved when an
operation does not own them; unknown enum values fail the affected transition closed.

A major change, removed field, changed invariant, or incompatible script contract requires a new
prefix (`relay:v2`), an explicit offline/drain migration, and no dual-write. A relay facing a
missing marker in a non-empty namespace, an unsupported major/range, or a script SHA/version it
does not recognize is unhealthy and rejects stateful routes with HTTP 503. It never guesses,
initializes over existing data, or falls back to memory.

## Valkey primitives and client

- **Hashes** hold bounded typed node, request, reservation, claim, progress, response, tombstone,
  terminal, and schema records.
- **Sorted sets** index epoch lease/deadline/retention expiry. They do not replace validation of the
  authoritative deadline in the Hash.
- **Streams and one consumer group per node** provide ordered, persistent, at-least-once queue
  delivery and pending-entry recovery. Consumer names are bounded random relay-instance IDs, not
  hostnames. Stream consumer ownership is evidence, while the claim Hash is the protocol owner.
- **Transactions/CAS** (`WATCH`/`MULTI`/`EXEC`) are reserved for bounded low-contention maintenance
  and schema administration. Retries are capped and jittered.
- **Reviewed server-side Lua scripts** implement multi-key protocol transitions. Scripts receive
  only validated keys/arguments, use server time, have bounded loops, declare a version/SHA, and
  are loaded and verified at startup. `EVALSHA` after `NOSCRIPT` may reload only an embedded,
  reviewed script. No downloaded or operator-supplied script is executed.
- **Pub/Sub** may wake blocked polls or cleanup workers after the durable transaction commits. A
  subscriber always rechecks Streams/records; lost, duplicated, or reordered notifications do not
  affect correctness. Pub/Sub is never authoritative.

The first backend uses the maintained `redis` Python package (`redis-py`) against the
Valkey-compatible protocol. The implementation PR must pin a reviewed compatible release and
prove Sentinel discovery/authentication, arm64 operation, pipelines/transactions, script
load/reload, reconnect after role changes, and real Valkey test-server support. These are release
gates, not optional conveniences.

## Atomic transition contract

All calls are state-machine operations; the store exposes no generic public key/value API. One
reviewed script performs each compound transition (several public operations may share a script):

1. **Register or renew.** Authenticate ownership by constant-time digest comparison, expire the
   old generation if necessary, enforce node count/capability bounds, write capabilities and lease,
   and update the lease ZSET. Registration returns a raw newly generated control credential once,
   but only its digest is stored. Renewal never changes owner or node generation.
2. **Validate eligibility and reserve capacity.** Reclaim a bounded batch of expired reservations,
   evaluate compatible model, sufficient context tier, healthy/non-draining state, smallest tier,
   least `(claimed + reserved)` load, round-robin tie break, and concurrency limit; increment the
   selected node's reserved count; create a unique reservation token record; and advance both
   global and tie-set fairness cursor in the same script. Selection without reservation is
   side-effect-free and is used only by availability reporting.
3. **Enqueue idempotently.** Validate an unexpired reservation bound to the same node, node
   generation, client/request identity and capacity; reject a conflicting existing identity;
   append the validated ciphertext envelope to the node Stream; create the pending request and
   deadline index; convert reserved capacity to queued capacity; and delete the reservation.
   Retrying the same identity and envelope digest returns the original result and Stream ID.
4. **Claim work.** Authenticate/renew the compute registration, reclaim expired claims in a bounded
   pass, select the oldest eligible Stream entry, atomically change request `queued` to `claimed`,
   create claim ownership/generation/lease, move queued to claimed capacity, and return the
   ciphertext. Concurrent pollers cannot both obtain a live claim for one request.
5. **Renew control/in-flight lease.** Authenticate node owner and claim generation, reject terminal
   or deadline-expired work, and extend the claim only up to the request deadline. Registration
   heartbeat and claim renewal are distinct fields even when one control call performs both.
6. **Accept encrypted response and finalize.** Authenticate the node and live claim, validate the
   client/request binding, choose `completed` only if no terminal winner exists, store exactly one
   encrypted response, clear progress/pending/claim and capacity, acknowledge/delete the Stream
   entry, write terminal outcome-once, and create the appropriate completed/unavailable control
   tombstone atomically. An exact response retry is successful/idempotent; a different response or
   losing terminal attempt receives a bounded gone/conflict result.
7. **Cancel, expire, unregister, or evict.** Authenticate requester proof digest or node owner as
   applicable; choose a terminal winner; remove queued/claimed/reservation ownership; release
   capacity; clear progress; and, for unregister/eviction, increment node generation and process a
   bounded batch. Remaining work is put on a bounded cleanup ZSET and stateful requests remain
   unavailable until cleanup completes, rather than becoming schedulable incorrectly.
8. **Create tombstone.** As part of transition 6 or 7, upsert only the winning bounded status,
   reason, owner/generation and deadline. Acknowledgement authenticates the owner and deletes only
   that tombstone; it cannot alter terminal outcome.
9. **Record a terminal outcome once.** `HSETNX`-equivalent scripted CAS chooses exactly one of
   completed, cancelled, or expired, increments bounded metrics bookkeeping once, and makes all
   later attempts observe the winner. This is exactly-one **accepted terminal transition**, not
   exactly-once request execution or delivery.
10. **Advance fairness cursor.** Cursor generation and next position advance in the same successful
    reservation script. Failed selection, availability checks, expired reservation reclaim, and
    idempotent retries do not advance it. Tie-set cursors use a bounded digest key and expire after
    inactivity; the global cursor remains authoritative when a tie-set cursor is absent.

### Reservation and delivery semantics

The current select-then-enqueue route sequence can over-select capacity when concurrent relays
interleave. Shared mode therefore requires the short-lived, single-use reservation token returned
by selection and supplied to enqueue. The token is opaque and bounded; only its digest is keyed.
Legacy clients that specify a target directly use an atomic reserve-and-enqueue operation, never a
non-reserving lookup. A reservation expiry script decrements reserved capacity only when token,
node generation, and state match, preventing double release. Abandoned reservations are reclaimed
opportunistically on selection and by a bounded sweeper.

Claims are at least once. After a claim lease expires, `XAUTOCLAIM` (or its reviewed equivalent)
and the claim script may deliver the same ciphertext again. A primary failure may lose recently
acknowledged asynchronous replication, so duplicate delivery is possible. Compute nodes must make
request handling safe to retry and responses idempotent by client/request identity. The terminal
CAS ensures only one terminal result is accepted, but neither Valkey nor `WAIT` supplies
exactly-once execution.

### API-v1 route mapping

| Active route | Atomic store operation(s) |
|---|---|
| `POST /api/v1/relay/servers/register` | register-or-renew node/lease |
| `POST /api/v1/relay/servers/unregister` | unregister/terminalize owned work in bounded batches |
| `POST /api/v1/relay/servers/poll` | renew registration; claim-or-reclaim work |
| `POST /api/v1/relay/servers/control` | authenticate and renew claim, or read/ack tombstone |
| `POST /api/v1/relay/requests` | consume reservation and idempotently enqueue, or atomic reserve-and-enqueue for explicit target |
| `POST /api/v1/relay/requests/cancel` | verify cancel digest and terminalize once |
| `POST /api/v1/relay/responses` | accept response and terminalize once |
| `POST /api/v1/relay/progress` | authenticate live claim and replace bounded encrypted progress |
| `POST /api/v1/relay/responses/retrieve` | atomically retrieve/consume response, or read pending/terminal and consume latest progress |
| scheduler selection used by API-v1 inference | reserve eligible capacity and advance cursor |

Retrieval is destructive only after the response has been returned under the existing API
contract. The implementation must use an explicit retrieval state/token so a connection failure
cannot silently turn an unreturned response into “unknown”; a retry either returns the same
response within retention or confirms its prior receipt. The exact HTTP handshake belongs in the
route-wiring slice, but the stored response and terminal outcome remain bounded.

## Privacy and relay-blind E2EE

Valkey may contain only: validated API-v1 ciphertext envelope fields (`ciphertext`, encrypted key,
IV, protocol/version); bounded encrypted progress/response fields; public routing keys only where
the peer protocol must receive them; identity and credential/proof digests; model identifiers and
bounded capability enums/counts; opaque reservation/consumer IDs or their digests; fixed status and
reason codes; Stream IDs; counters; schema/script versions; and UTC epoch timestamps/deadlines.

It MUST NOT contain plaintext prompts/messages, tool arguments or output, model output, decrypted
payloads, raw control credentials, raw cancellation proofs, relay private keys, Kubernetes Secrets,
connection strings, arbitrary request dictionaries, exception text, URLs, host/node names, or
unbounded labels. Ciphertext bytes are payload data and may be stored only in the encrypted
envelope records, never logs, metrics, traces, diagnostics, or errors.

Logs, errors, metrics, health/availability diagnostics, traces, and key names use fixed operation,
reason, status, schema, and bounded-count allowlists. They may include non-reversible short
fingerprints only where the existing security review permits them; they never include raw keys,
request IDs, credentials, addresses, or any envelope field. Backend failures become a fixed error
code and do not echo client/server messages or connection details.

## Operations and failure policy

### Persistence, memory, and failover

The HA deployment enables AOF with `appendfsync everysec` and periodic RDB snapshots. It does not
retain long-term backups or treat coordination state as durable history. AOF rewrite and snapshot
storage have explicit disk bounds and alerts. Restarts load local persistence before joining; an
empty or stale node cannot be promoted until it has synchronized from the current primary. Startup
automation must not bootstrap an empty primary while replicas contain newer useful state. Total
loss still loses accepted work and requires client retry/re-registration.

Valkey uses a configured `maxmemory` with `noeviction`. Admission bounds and cleanup keep usage
below an alert threshold. A rejected/OOM write fails the whole transition and produces bounded HTTP
503; the relay never deletes live coordination records or treats a partial write as accepted.

Sentinel is the discovery authority: clients resolve the configured service name through multiple
Sentinel endpoints, verify ACL/TLS policy, and reconnect when a connection becomes read-only,
changes role, or closes. Each operation has bounded connect/socket timeouts and a small capped,
jittered retry budget. Only operations with an idempotency identity may be retried after ambiguous
write results; otherwise the client reads transition state before retrying. Stale pooled
connections are discarded after failover. `WAIT` for a configured replica count may be used after
high-value writes to reduce loss probability, with a bounded timeout and explicit failure policy,
but replication is asynchronous: `WAIT` does not provide strong consistency, prevent acknowledged
write loss in every failover, or create exactly-once semantics.

### ACL

Credentials are injected from Secrets and never embedded in URLs, values, logs, or this ADR. The
relay ACL is restricted to the two configured prefixes and the minimum command families required:
connection/authentication, read/write Hashes, ZSETs, Streams/consumer groups, key TTL/deletion,
`TIME`, transactions/watch, `SCRIPT LOAD`/`EVALSHA`, and optional `PUBLISH`/`SUBSCRIBE` plus `WAIT`.
It denies administrative, configuration, flush, module, debug, arbitrary `EVAL`, broad key scan,
and access outside the prefixes. Deployment automation/monitoring uses separate identities.

### Health and degraded behavior

`/livez` remains process-only. `/healthz` checks a bounded ping, role/discovery, schema range, and
required script versions, returning HTTP 503 with `Cache-Control: no-store` and fixed reasons when
the configured backend is unavailable or incompatible. Zero schedulable compute nodes does not
make the pod unready; functional capacity belongs to the later availability endpoint and metrics.
Stateful API-v1 operations fail closed with a bounded 503 and `Retry-After`; no response contains
addresses, credentials, keys, IDs, payloads, or exception text. Retries are bounded as above.
Graceful shutdown stops selection/claims locally while accepted shared work remains available.

Shared mode is mandatory when relay replicas or worker processes exceed one. Both lifecycle state
and Flask-Limiter storage must use the shared, environment-specific prefixes. Direct rate-limit
owner lookups move behind the state store. Startup/chart validation rejects multiple replicas or
workers with memory lifecycle state, process-local limiter storage, or enabled legacy routes.
There is no process-local fallback after a Valkey error.

## Migration, rollback, and required proof

Staging drains current accepted work, deploys one relay replica configured for Valkey, initializes
the empty versioned namespace, and lets compute clients re-register. It never dual-writes memory
and Valkey. After one-replica restart recovery and shared rate limits pass, staging may scale to
multiple node-spread relays. Rollback scales to one relay replica **while retaining Valkey**; it
does not switch an active namespace back to memory and strand accepted work.

Before replicas scale above one, the same contract suite must pass against memory and a real Valkey
server for bounds, TTL/deadline expiry, renewal, reservation/fairness, queue ordering/wakeup,
idempotent enqueue, claims/reclaim, response retrieval, cancellation/unregistration/expiry races,
tombstones, terminal deduplication, progress, cleanup, and schema compatibility. Independently
constructed relay instances sharing one backend must prove register on A, reserve/enqueue on B,
poll/control on C, respond on A, and retrieve/cancel on B or C.

Concurrency/failure tests must cover duplicate identities, simultaneous reservations and claims,
response-versus-cancel, unregister-versus-poll, lease expiry, abandoned reservations, relay death,
store interruption/restart, primary and node failover, stale connections, compatible rolling
deployments, and compute re-registration. Failover assertions must permit duplicate delivery but
require one accepted terminal transition and bounded recovery. E2EE tests inspect datastore
keys/values and every error/log/metric/diagnostic/trace surface against the allowlists above. The
selected client and images must also pass Sentinel and arm64 tests.

## Consequences and non-blocking follow-ups

This decision enables the next bounded implementation slice: extend the typed store contract and
memory backend with reservation plus idempotent enqueue, including backend-neutral concurrency and
expiry tests. Subsequent slices implement the Valkey scripts/backend, route wiring, availability,
shared limiter integration, and finally chart/runbook changes and failover exercises.

Non-blocking choices that do not alter the data model are deployment-specific reductions from the
contract bounds/TTLs, optional Pub/Sub channel layout, whether high-value operations use `WAIT`,
monitoring thresholds, and the reviewed compatible `redis-py` patch version. They must be fixed in
configuration and tested before deployment. No core key family, atomic boundary,
delivery guarantee, persistence mode, discovery model, migration rule, or fail-closed behavior is
left undecided.
