# ADR: compute registration and lease state-store boundary

- **Status:** accepted as a prerequisite design slice
- **Date:** 2026-08-19
- **Refs:** issue #1569

## Context

The relay currently coordinates compute nodes with process-local state in `relay.py`. That state
cannot provide cross-process correctness and prevents a future highly available relay from serving
one protocol through multiple replicas. A typed boundary is needed before a shared Valkey-compatible
implementation can be designed and reviewed. The boundary must also preserve the relay-blind E2EE
rule: relay coordination may contain safe routing metadata and ciphertext, but never plaintext model
payloads.

## Decision

Introduce a `RelayStateStore` protocol and a lock-protected `InMemoryRelayStateStore`. This first
boundary is deliberately limited to compute-node registration, lease renewal, lookup/listing,
authoritative expiry, and explicit unregistration. It is not wired into `relay.py`; existing routes,
scheduling, health, metrics, rate limits, and runtime behavior therefore remain unchanged.

The public API models protocol transitions rather than exposing general key/value operations.
Registration and capability records are frozen typed values, and collections are immutable tuples,
so reads cannot mutate backend state. Register, renew, expire, and unregister transitions execute
under one memory-backend lock. Duplicate registration with the same credential digest renews the
lease and may replace the complete validated capability snapshot; a different digest conflicts.
Renewal of an unknown or boundary-expired node returns no record. Expiry is inclusive at
`lease_expires_at_epoch <= now`, returns sorted node IDs, and unknown-node unregistration is an
idempotent `false`. Registration capacity is checked after atomic expiry, permitting an expired slot
to be reused.

Every store instance requires a bounded environment/cluster namespace, the one supported schema
version, an authoritative bounded lease TTL, and a maximum registration count. Records carry schema
version and UTC epoch registration/expiry deadlines. The clock is injectable for deterministic
tests; Python `time.monotonic()` values are neither persisted nor exposed. Future remote backends
should use their authoritative server time/TTL while presenting the same epoch-deadline semantics.

Only the scheduler metadata validated by the current API v1 capability model is retained: API
version, bounded model IDs, context tier and token limit, output reservations, concurrency, and a
fixed backend-class enum. The limits intentionally mirror the current relay (64 model IDs, 128-byte
IDs, concurrency up to 128, supported context tiers, and positive bounded token counts). Records
contain only a lowercase SHA-256 control-credential digest; callers generate and return raw
credentials outside the store. The types have no fields for prompts, messages, tool data, model
output, relay private keys, raw credentials, or arbitrary payload dictionaries. This narrow shape is
the E2EE fail-closed boundary, not an inference-content store.

Backend contract tests construct stores through a factory fixture. A future Valkey implementation
will supply another factory and run the same behavioral suite, then add backend-specific integration
and failure tests without weakening the shared contract.

## Deliberately deferred

This decision does **not** define or migrate runtime routes. It also defers encrypted queues,
capacity reservations, claims and reclaim, responses, cancellation, tombstones, terminal-outcome
deduplication, scheduler cursors, shared rate limits, functional availability, Valkey data
structures or server-side scripts, Sentinel discovery, and schema-compatible rolling-upgrade policy.
Those transitions require separate protocol design and atomicity review; prematurely adding them to
this small contract would constrain that work without proving distributed correctness.

The in-memory backend remains process-local. This slice does not make the relay horizontally
scalable, does not introduce Valkey or any deployment change, and does not satisfy issue #1569 by
itself. It is only the bounded prerequisite for later runtime migration and shared-backend work.

## Consequences

The repository gains a reviewable state-machine vocabulary and reusable backend conformance tests
without changing production behavior or adding dependencies. There is intentional duplication
between the new capability validation and the current relay helper until route migration can move
both callers behind one boundary; keeping this slice unwired avoids an unsafe partial migration.
