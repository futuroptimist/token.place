# ADR: Valkey shared relay state and atomic transitions

- **Status:** accepted design; not implemented
- **Date:** 2026-08-19
- **Issue:** [#1569](https://github.com/futuroptimist/token.place/issues/1569)
- **Supersedes:** the Valkey topics explicitly deferred by the
  [registration/lease boundary ADR](relay_state_store_registration_lease_adr.md); that ADR remains
  authoritative for its implemented memory-store contract

## Decision and scope

The first shared backend will use Valkey, reached through Sentinel, as the authoritative ephemeral
coordination store for API v1 relay work. The Python client will be the maintained `redis-py`
package (`redis`, including `redis.sentinel.Sentinel`); the implementation must pin a reviewed
version and prove Sentinel discovery, arm64 operation, transactions/scripts, reconnect, and a real
Valkey test server before deployment. This choice does not add a dependency in this design-only
change.

This ADR fixes the data model and transition boundaries needed by later contract and backend
slices. It does **not** implement Valkey, wire routes, change dependencies or Kubernetes resources,
or make the relay highly available. Current runtime state remains process-local and current
single-process behavior is unchanged. In particular, one standalone Valkey pod is not HA.

The store exposes typed state-machine operations, never general-purpose key/value methods. All
limits below are configuration values with safe finite defaults; implementations may make them
smaller, but may not remove them. Validation happens before a transition and a rejected transition
has no side effects.

## Current protocol inventory

The design follows the actual `relay.py` API v1 sequence rather than introducing a different
queue protocol:

1. `/servers/register` creates or heartbeats `known_servers`, stores capabilities and a control
   credential digest, and returns the raw credential only to the compute node.
2. `/servers/next` filters by model, context tier, health/draining state, smallest capable tier,
   least queue-plus-in-flight load, round-robin tie break, and concurrency.
3. `/requests` currently performs a separate lookup and appends an E2EE envelope to the selected
   node's list while creating pending identity, cancel proof, and deadline state.
4. `/servers/poll` renews registration/capabilities, removes the next queue item, and installs an
   in-flight owner/lease. `/servers/control` renews or acknowledges that work and observes
   cancellation/expiry tombstones.
5. `/progress` replaces the latest encrypted progress envelope for owned in-flight work.
6. `/responses` accepts one encrypted final response, removes queued/in-flight/pending/progress
   state, and records completion once. `/responses/retrieve` destructively retrieves the response,
   otherwise returns pending progress or a terminal result.
7. `/requests/cancel`, deadlines, unregister, and stale-node eviction race with poll and response;
   they remove live work, create owner-visible tombstones, and record one terminal outcome.

### Current-to-shared-state mapping

| Current process-local state | Shared authoritative record/index | Transition use |
|---|---|---|
| `known_servers` registration, capabilities, control digest, `last_ping`, and node state | bounded node hash plus lease sorted set | register/renew, select, poll/control, unregister/evict |
| `server_round_robin_next_index` and `api_v1_filtered_round_robin_next_positions` | scheduler hash keyed by policy and normalized eligibility-set digest | reserve and advance fairness cursor |
| `client_inference_requests` lists and condition | one bounded Stream per node; optional Pub/Sub wakeup | enqueue, claim, remove/reclaim |
| per-node `api_v1_in_flight_requests` and legacy single in-flight fields | request hash, claim hash, claim-expiry sorted set, and Stream pending-entry state | claim, renew, respond, cancel, reclaim |
| `client_pending_request_ids`, deadlines, and cancel tokens | request hash and deadline sorted set; cancel-token **digest** only | enqueue, retrieve, cancel, expire |
| `client_responses` | response hash referenced by request plus retrieval TTL/state | accept response, retrieve once |
| `client_progress` | bounded latest-progress hash referenced by request | progress replace, retrieve, finalize |
| `client_terminal_request_ids` and `client_terminal_outcomes` | one terminal hash with outcome/dedup TTL | exactly-one terminal transition and metric event |
| `api_v1_control_tombstones` and `api_v1_recently_unregistered_servers` | bounded control and node tombstone hashes with expiry indexes | cancel/expire/unregister control, late-owner authentication |
| API public/control-plane in-memory limiter storage and `_control_server_owner_identity` lookup | separately prefixed shared fixed-window buckets; owner digest resolved through node state | all limited routes |
| `streaming_sessions` and `streaming_sessions_by_client` | **none** | legacy-only; forbidden in shared-state HA mode |

Locks, conditions, process shutdown/draining flags, static configuration, injected Secrets,
Prometheus registries, HTTP timers, and local connection pools remain process-local. Their values
are never correctness evidence. Gauges are derived from bounded shared aggregates or scans rather
than local collections.

## Namespace, schema, and compatibility

Every key starts with the operator-configured, routing-safe prefix
`tp:{environment}:{cluster}:relay:v{schema}:`. `environment` and `cluster` are opaque normalized
deployment identifiers, not network addresses. The hash tag encloses both identifiers so keys
used by one atomic script share a cluster slot if a clustered deployment is evaluated later. API
v1 coordination suffixes begin `state:`; rate limits begin `ratelimit:` and use the same environment
and cluster namespace. User-controlled strings never appear directly in keys: node, client, and
request components are fixed-length SHA-256 digests of their canonical identifiers.

`tp:{environment}:{cluster}:relay:schema` is a small unversioned bootstrap hash containing
`active_schema`, `minimum_reader`, `minimum_writer`, and a deployment generation. Startup and
`/healthz` read it before serving stateful routes. Schema 1 readers and writers use only `v1` keys.
Additive fields must be ignored by older readers, have safe defaults, and retain old write meaning.
A rollout first raises compatible reader capability, then writers, and only later uses an additive
field. A semantic/key-layout change requires a new version prefix and an explicit offline or
drain-and-cut-over migration; a process outside the advertised reader/writer interval rejects the
backend. It must not read another version, create an empty namespace, or fall back to memory.

No application dual-writing is permitted. The schema bootstrap update is CAS-protected and only a
designated migration job may change it. Unknown enum values, missing required fields, or a newer
incompatible schema produce a bounded `state_schema_incompatible` failure.

## Records, bounds, TTLs, and time

The implementation will publish exact byte and count constants in the typed contract. Initial
ceilings are: 1,024 live nodes; 64 model IDs per node; 128 concurrent claims per node; configured
per-node queue depth; one request/response/progress envelope no larger than the existing HTTP body
limit; and finite global live-request, tombstone, and rate-bucket counts. Streams use exact or
conservative `MAXLEN` only as secondary hygiene—live entries are never trimmed merely to meet a
memory target. Admission rejects before any count limit is exceeded.

| Family | Authoritative representation | Expiry policy |
|---|---|---|
| Registration/capabilities | node hash; lease deadline in node-lease sorted set | server-time lease TTL; expiry transition removes the node only after handling its work |
| Cursor/reservation | scheduler hash; reservation hash and expiry sorted set | reservation token TTL, initially the lesser of 15 seconds and one-half node lease |
| Queue | per-node Stream entry containing encrypted envelope and bounded routing fields | request deadline; stream/key TTL extends only to the latest live entry plus terminal grace |
| Pending/deadline | request hash and global deadline sorted set | accepted UTC epoch deadline, maximum one hour under current API bounds |
| Claim/in-flight | claim hash, claim-expiry sorted set, Stream PEL/consumer group | short renewable claim lease, never beyond request deadline |
| Response/retrieval | encrypted response hash and response-expiry sorted set | terminal retention TTL, initially current 300-second terminal TTL; deleted on successful retrieval |
| Progress | latest encrypted progress hash | no later than claim/request deadline; deleted at terminal transition |
| Control/cancellation | tombstone hash and expiry sorted set | current bounded control TTL, at most 300 seconds |
| Terminal/dedup | terminal hash and expiry sorted set | current terminal retention, initially 300 seconds |
| Rate limits | fixed-window counter keys | window TTL plus bounded clock-skew margin |

Datastore `TIME` is the transition clock. Records persist UTC Unix epoch milliseconds for deadlines
and audit-safe ages; TTLs are set in the same atomic operation. Python `time.monotonic()` values are
never persisted or compared across processes. Expiry indexes make cleanup discoverable; key TTLs
are a backstop, not the only transition mechanism. A worker encountering an overdue record performs
the corresponding atomic expiry/reclaim operation before returning a result.

## Data structures and atomicity

- **Hashes** hold bounded typed records and small scheduler metadata. They avoid opaque serialized
  Python objects and permit compatible additive fields.
- **Sorted sets** index absolute lease, deadline, reservation, claim, response, and tombstone expiry
  epochs. Scores are server-time epoch milliseconds; members are fixed digests.
- **Streams with one consumer group per node queue** preserve append order and give at-least-once
  delivery through pending entries and `XAUTOCLAIM`. A claim is not authoritative without its
  matching request/claim hash. Stream IDs are routing metadata only.
- **Transactions/CAS** (`WATCH`/`MULTI`/`EXEC`) are used for simple, low-contention schema and
  administrative updates. Retried closures are bounded by attempt count and wall time.
- **Reviewed server-side scripts** implement multi-key protocol transitions. Scripts receive only
  explicit prefixed keys and bounded arguments, use `redis.replicate_commands()`/Valkey-supported
  deterministic behavior as applicable, and return fixed result enums. Each script is source
  controlled, SHA-verified at load, invoked by `EVALSHA`, reloaded once after `NOSCRIPT`, and tested
  against real Valkey. Application-composed `EVAL` is forbidden.
- **Pub/Sub** may wake a blocked poll or retrieval after durable mutation. Lost, duplicated, or
  reordered notifications merely cause bounded polling; Pub/Sub is never authoritative state.

## Atomic transition contract

Each numbered operation corresponds to issue #1569's required transition list.

1. **Register or renew node and lease.** A script validates schema, capacity, owner digest, and
   capability bounds; gets server time; creates or updates the node hash and lease index; preserves
   the original owner digest on renewal; sets TTLs; and returns the lease epoch. A raw control
   credential is generated outside Valkey, returned once, and only its digest is passed in.
2. **Validate eligibility and reserve capacity.** One script removes expired reservations/claims,
   evaluates the current policy snapshot (compatible normalized model, sufficient tier,
   healthy/non-draining, smallest tier, least `queued + claimed + reserved` load, then cursor),
   rejects at `max_concurrency`, creates a random 256-bit reservation-token **digest** bound to the
   node/model/tier, and advances both global and eligibility-set cursors. The caller receives the
   raw opaque token. This bounded reservation is required: the current separate `/servers/next`
   and `/requests` calls otherwise over-reserve concurrently.
3. **Idempotently enqueue encrypted request.** A script keys identity by the digest of canonical
   client key plus request ID. It verifies schema, live reservation, target binding, node lease,
   envelope size, deadline, queue/global bounds, and cancel-proof digest; `XADD`s once, creates the
   pending request/deadline indexes, consumes the reservation, and increments queued load. A retry
   with the same identity and identical envelope digest returns the original accepted result; a
   conflicting digest is rejected. For a compatibility window `/requests` may omit a token only in
   single-process memory mode. Shared mode requires it, so old clients fail explicitly rather than
   race.
4. **Claim and lease work.** Poll first renews the authenticated node lease, then a script uses the
   group to obtain or reclaim the oldest eligible entry, verifies request/node state and capacity,
   changes `queued` to `claimed`, records node/consumer/claim-token digest and claim expiry, and
   updates load exactly once. Expired/cancelled entries are acknowledged and terminalized instead
   of delivered. A raw claim token accompanies the envelope and is required for later ownership
   operations.
5. **Renew control/in-flight lease.** One script authenticates node and claim digests, checks the
   nonterminal request and deadline, renews node and claim expiry (not beyond the request deadline),
   and optionally acknowledges a terminal tombstone. It returns only fixed control status and
   bounded deadline metadata.
6. **Accept response and finalize.** One script authenticates the claiming node/claim, rejects an
   overdue or terminal request, stores the bounded encrypted response, changes the request exactly
   once to `completed`, records the dedup outcome, removes pending/claim/progress/reservation state,
   decrements load, acknowledges/deletes the Stream entry, and installs response/terminal TTLs.
   Repeating the same accepted response digest is a successful idempotent replay; a different
   response or competing cancellation receives a fixed gone/terminal result.
7. **Cancel, expire, unregister, or evict.** A parameterized script validates the appropriate
   cancel-proof, owner credential, or server-time deadline; wins only from a live state; removes
   queue/claim/reservation/progress state; adjusts load; and enumerates affected work for a bounded
   node batch. Unregister/eviction repeats in bounded batches and marks the node draining so no new
   reservations enter between batches.
8. **Create tombstone.** The winning transition atomically creates the node/request control
   tombstone with owner digest, fixed status/reason, deadline, and TTL. Tombstone creation is part
   of operations 6 or 7, not a later best-effort write.
9. **Record one terminal outcome.** The same terminal script uses create-if-absent semantics for a
   single terminal record. Only its winner emits `outcome_recorded=true`; callers increment the
   process metric from that flag, so retries do not double-count. Terminal state, not the metric,
   is authoritative.
10. **Advance fairness cursor.** Reservation advances the global cursor and the normalized
    eligibility-set cursor in the same script. Claim/enqueue do not advance it again. Expired or
    abandoned reservations are removed and load decremented before the next selection; reclaim
    does not rewind a cursor. Eligibility cursor hashes have bounded LRU/TTL cleanup.

Reservation creation and enqueue are deliberately separate because the active API exposes
`/servers/next` followed by `/requests`. Binding and consuming a short-lived token closes that
race without putting ciphertext in selection state. If enqueue fails, the reservation expires and
is reclaimed. Capacity counts reservations, queued items, and active claims, preventing concurrent
selectors from exceeding node concurrency.

## API v1 route mapping

| Active route | Store operations |
|---|---|
| `POST /api/v1/relay/servers/register` | register-or-renew node/lease |
| `POST /api/v1/relay/servers/unregister` | authenticate and drain; cancel/terminalize bounded batches; unregister and tombstone |
| `GET /api/v1/relay/servers/next` | expire/reclaim due reservations; select-and-reserve; advance cursor |
| `POST /api/v1/relay/requests` | verify/consume reservation and idempotently enqueue/pending-create |
| `POST /api/v1/relay/servers/poll` | renew node; claim-or-reclaim next work |
| `POST /api/v1/relay/servers/control` | renew claim or read/acknowledge control tombstone |
| `POST /api/v1/relay/progress` | authenticate active claim and replace bounded encrypted progress |
| `POST /api/v1/relay/responses` | accept encrypted response and win terminal completion |
| `POST /api/v1/relay/responses/retrieve` | expire if due; atomically retrieve/delete response, or read pending/latest progress/terminal |
| `POST /api/v1/relay/requests/cancel` | verify cancel-proof digest and win cancel terminal transition |

The API v1 chat/source guardrail routes remain disabled until their E2EE runtime migration. The
availability endpoint requested by #1569 will perform a side-effect-free eligibility read: it does
not reserve or advance cursors.

## Delivery and failover semantics

Claims are **at least once**. An expired claim is recoverable through the Stream pending list and
claim-expiry index. The same compute node or another authenticated instance for that registration
may receive it again. Enqueue is idempotent by client/request identity and envelope digest, but a
network failure can hide a successful enqueue and therefore clients must retry with the same
identity. A failover may redeliver already-observed work. Compute clients must treat request IDs as
idempotency keys and tolerate reclaim.

There is exactly one **accepted terminal transition in the surviving primary history**, enforced
by the request state machine; this is not exactly-once execution or delivery. Valkey primary-to-
replica replication is asynchronous. Optional `WAIT` after an accepted mutation may reduce the
probability of losing it before failover, but adds latency and neither creates strong consistency
nor makes delivery or terminal acceptance globally exactly once. If an acknowledged write is lost
during promotion, retry/reclaim and idempotency bounds the damage; tests must exercise this case.

Clients use Sentinel to discover the current primary. On read-only, connection, timeout, or role
errors they discard the connection, rediscover, and retry only operations whose contract supplies
an idempotency key. Retries use exponential backoff with jitter, a small attempt ceiling, and a
total duration shorter than the HTTP 503 budget and relevant lease. Non-idempotent ambiguity is
resolved by reading request state, never blind replay. Stale connections after failover must
recover without an operator restart.

## Persistence, memory, and HA deployment contract

Although coordination is ephemeral, routine restart must not erase accepted live work. The HA
deployment therefore enables AOF with `appendfsync everysec` and periodic RDB snapshots on primary
and replicas. Long-term backups and point-in-time chat recovery are out of scope. Promotion is
Sentinel-controlled from a sufficiently caught-up replica; automation must not bootstrap or
announce an empty restarted node as primary while replicas retain data. Startup waits for the
configured dataset/schema and fails closed on an empty unexpected generation. Planned full reset
requires an explicit drained namespace-generation change.

Production/staging uses a primary, replicas, and at least three independent Sentinel voters (or a
separately reviewed maintained HA operator), distributed across failure domains. A standalone pod
is development infrastructure, never HA. The client accepts Secret-injected Sentinel service names,
TLS/auth material, and timeouts; no credentials or environment addresses belong in code, values
examples, errors, or this ADR.

Valkey uses bounded `maxmemory` and `maxmemory-policy noeviction`. Reaching memory or record limits
rejects writes and the relay returns bounded 503/429-style fixed errors as appropriate; it never
evicts live coordination records silently and never falls back to process memory. Alerts must fire
before the limit. Cleanup scripts, TTLs, Stream acknowledgements, and admission counts keep every
family bounded.

The least-privilege relay ACL is restricted to its namespace and the commands needed for
`GET`, `SET`, `DEL`, `EXISTS`, `EXPIRE`/`PEXPIRE`, `TTL`/`PTTL`, hashes, sorted sets, Streams and
consumer groups, `TIME`, `WATCH`/`MULTI`/`EXEC`/`UNWATCH`, `EVALSHA`/`SCRIPT LOAD`/`SCRIPT EXISTS`,
`PUBLISH`/`SUBSCRIBE` when wakeups are enabled, `WAIT` when configured, and connection health.
Administrative, keyspace-wide, module, config, shutdown, flush, arbitrary script, and cross-prefix
access are denied. ACL usernames/passwords and TLS keys remain Kubernetes Secrets.

## Relay-blind E2EE allowlist

Valkey may contain only:

- encrypted API v1 request, response, and progress envelope fields already accepted by the strict
  relay schema (ciphertext, encrypted content key, IV, protocol/version);
- bounded normalized capabilities and fixed scheduler enums/counts;
- fixed-length digests of node/client/request identifiers, credentials, reservation/claim/cancel
  proofs, and envelope content;
- Stream IDs, bounded server-time epochs/TTLs, sizes, sequence numbers, and schema generations;
- fixed terminal/control statuses and reasons from reviewed enums; and
- bounded rate-limit counters and windows under the separate prefix.

Valkey must never contain plaintext prompts/messages/responses, tool names/arguments/output, model
output, raw public or private keys, raw request IDs, raw credentials/proof tokens, arbitrary HTTP
bodies/headers, client IPs, URLs, hostnames, traces, exceptions, or environment addresses. Relay
private keys never enter shared state. Ciphertext is allowed in datastore values but never in keys,
logs, errors, metrics, diagnostics, or traces.

Errors expose fixed codes and retry hints only. Logs use fixed operation/reason and, only when
needed, non-reversible bounded fingerprints. Metrics use enumerated operation/result labels and
aggregate counts/latencies. Diagnostics expose schema/backend status and bounded counts only.
Tests inspect all keys and decoded record fields plus captured errors, logs, metrics, diagnostics,
and traces against these allowlists.

## Health, rate limits, and legacy routes

`/livez` remains process-only. `/healthz` checks bounded connection latency, writable-primary role,
and schema compatibility and returns HTTP 503 with `state_backend_unavailable` or
`state_schema_incompatible` when required shared state is unusable. It does not require a compute
node; functional capacity belongs to `/api/v1/relay/availability`. Stateful API routes fail closed
with a small fixed 503 body and `Retry-After`, within configured connect/command/retry deadlines,
without addresses, credentials, keys, payloads, or raw datastore errors. Shutdown stops selection
and new claims locally while accepted shared work remains reclaimable.

Both public and compute-control fixed-window limits use the shared
`tp:{environment}:{cluster}:relay:v1:ratelimit:` prefix and bounded digested identities. Node owner
identity is resolved through the state-store operation, not direct `known_servers` access. If relay
replicas or workers exceed one, shared rate limiting and shared coordination are mandatory;
process-local fallback is prohibited during startup and outages. Single-process development may
explicitly select the memory backend.

`streaming_sessions` and `streaming_sessions_by_client` serve deprecated legacy streaming routes,
not active non-streaming API v1. They stay local and are not migrated. Shared-state/HA configuration
must reject startup when legacy relay routes are enabled. The deprecated `/sink`, `/faucet`,
`/source`, `/retrieve`, and `/next_server` routes receive no shared-state compatibility path.

## Migration, rollback, and required proof

Staging migration is drain-and-cut-over, never dual-write:

1. implement and run the expanded store contract against memory and a real Valkey server;
2. wire all API v1 operations and both limiter families behind the store, with shared-mode startup
   validation and health checks;
3. deploy the reviewed Sentinel topology and schema bootstrap while the relay remains one replica;
4. stop admission, drain or explicitly expire existing memory work, restart one relay on Valkey,
   and let compute nodes re-register;
5. prove persistence/restart and one-replica operation, then scale to multiple node-spread replicas
   only after the tests below pass.

Rollback after cut-over scales the relay to one replica **while retaining Valkey and its namespace**.
It must not switch back to memory and strand accepted work. Schema rollback is permitted only while
the prior binary remains within the bootstrap compatibility interval; otherwise drain and perform
an explicit generation cut-over.

Before replicas exceed one, the same backend contract must cover TTL/expiry, renewal, selection,
reservation expiry, queue order/wakeup loss, idempotent enqueue, claim/reclaim, progress, response
retrieval, every terminal race, tombstones, deduplication, and cleanup bounds. Multi-instance tests
construct independent relay applications against one store and perform register on A,
select/reserve/enqueue on B, poll/control on C, response on A, and retrieve/cancel on B or C.

Concurrency/failure suites must cover duplicate identities and conflicting envelopes, simultaneous
selection and claims, response-versus-cancel, unregister-versus-poll, deadline/lease/reservation
expiry, abandoned reservations, relay termination, network interruption, stale connections,
primary failover, backend restart, compatible rolling binaries, and compute re-registration. They
must assert at-least-once recovery, capacity never exceeding reservations plus live work, one
accepted surviving terminal result, bounded retry/503 behavior, and the E2EE allowlists.

## Non-blocking follow-ups

The exact reviewed `redis-py` version, numeric memory budget, Sentinel/operator manifests, alert
thresholds, and whether a latency-sensitive deployment enables `WAIT` are deployment measurements,
not data-model decisions. A future Valkey Cluster evaluation may require a different key layout and
therefore a new schema/ADR; schema 1 assumes a single Sentinel-managed primary. API v2 and durable
history remain out of scope.
