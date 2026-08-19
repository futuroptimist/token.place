# ADR: registration and lease state-store boundary

**Status:** Accepted as a prerequisite for issue #1569; deliberately incomplete.

## Decision

Introduce a typed `RelayStateStore` boundary and a memory-only implementation for compute-node
registration, lease renewal, lookup/listing, expiry, and explicit unregistration. The API expresses
protocol transitions rather than generic key/value access. Immutable records contain only a node
identifier, a control-credential digest, bounded API-v1 scheduler capabilities, an epoch lease
deadline, and a schema version.

The store configuration makes the environment/cluster namespace, schema version, authoritative
lease TTL, maximum node count, and injectable UTC epoch clock explicit. Deadlines use epoch time;
Python `time.monotonic()` values are neither persisted nor exposed. Expiry occurs when the deadline
is equal to or earlier than the current clock. Registering an existing live node renews its lease
and may replace validated scheduler capabilities, but preserves the existing credential digest.
Renewal of an unknown or expired node returns no record. Unregistering an unknown node returns
`false`. A new registration fails at the configured capacity bound. Each transition is atomic in
the memory backend.

Capabilities reuse the scheduler's API-v1 tiers and bounds: bounded model identifiers, token
limits, concurrency, and backend class. Records are immutable and reads return defensive records.
The API cannot accept arbitrary payload dictionaries, raw control credentials, prompts, messages,
tool data, model output, relay private keys, or other application content. This keeps the boundary
relay-blind: only safe routing and scheduling metadata plus a credential digest enters the store.

## Context and consequences

Issue #1569 requires correctness-critical relay coordination to become backend-independent before
multiple relay processes can share it. Defining the narrow contract first lets later backends use
the same behavioral suite through its store-factory fixture. This slice is memory-only and
registration/lease-only so the transition semantics can be reviewed without adding a dependency,
changing deployed behavior, or prematurely coupling the contract to Valkey primitives.

The new implementation is intentionally not wired into `relay.py` or `api/__init__.py`; current
routes, scheduler behavior, health/readiness, rate limits, metrics, and process-local globals remain
unchanged. Consequently, this decision does **not** make the relay horizontally scalable and does
not satisfy issue #1569 by itself.

## Deferred decisions

Later slices must separately decide and test runtime route migration; queues; claims and reclaim;
encrypted responses; cancellation; tombstones; terminal transitions and outcome deduplication;
scheduler cursors and capacity reservations; shared rate limits; functional availability; Valkey
data structures, transactions, and scripts; Sentinel discovery; and schema-compatible
rolling-upgrade policy. Deployment topology, persistence, failure behavior, and migration/rollback
also remain outside this ADR. No inference queues or ciphertext envelope shapes are implied by this
initial contract.
