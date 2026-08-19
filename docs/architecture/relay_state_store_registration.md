# ADR: registration and lease boundary for relay state

- **Status:** Accepted (prerequisite slice)
- **Date:** 2026-08-19
- **Issue:** [#1569](https://github.com/futuroptimist/token.place/issues/1569)

## Context and decision

Correctness-critical relay coordination currently lives in process-local state. A typed
`RelayStateStore` boundary is needed before that state can safely move to a shared backend. This
first slice deliberately covers only compute-node registration, lookup/listing, lease renewal and
expiry, and explicit unregistration. It provides an in-memory implementation and does **not** wire
the boundary into relay routes, change runtime behavior, or make the relay horizontally scalable.
It is a prerequisite, not a solution to #1569 by itself.

The public interface exposes protocol transitions rather than generic key/value access. Inputs and
outputs are frozen typed records, so callers cannot attach arbitrary dictionaries or mutate stored
state through a returned value. Duplicate registration by the same credential digest atomically
renews the lease and may replace valid scheduler capabilities; a conflicting digest fails. Renewal
of an unknown or boundary-expired node fails, while removal of an unknown node is an idempotent
`false`. The memory backend serializes register, renew, expiry, and unregister transitions with one
re-entrant lock.

## Data and compatibility rules

Each store instance has an explicit, bounded environment/cluster namespace and exact schema
version. Its authoritative registration TTL, maximum record count, node identifier size, and
capability sizes are explicit and validated. The capability record retains only the normalized
API-v1 scheduler fields and follows the current limits for model count and length, context tiers,
token counts, concurrency, and backend class.

Persistable deadlines use an injectable UTC epoch clock. Python `time.monotonic()` values are not
part of the record. A lease is expired when its deadline is less than or equal to the current epoch
time, and reads remove expired records before returning. Records contain only the SHA-256 control
credential digest; the raw credential is neither accepted nor returned.

The API cannot accept prompts, messages, tool data, model output, relay private keys, raw control
credentials, ciphertext queues, or arbitrary unbounded payload dictionaries. This narrow surface
preserves the relay-blind E2EE invariant and prevents registration state from becoming an
application-payload store.

## Backend verification

The behavioral tests are organized around a store-factory fixture. A future Valkey implementation
will supply another factory and run the same registration/lease suite, including deterministic
boundary expiry, bounds, immutable reads, duplicate behavior, and concurrent transitions. The
shared suite is necessary but will not replace Valkey integration, failure, and failover tests.

## Deliberately deferred

This decision does not define or migrate runtime routes. It also defers queues, claims and reclaim,
responses, cancellation, tombstones, terminal transitions and outcome deduplication, scheduler
cursors and reservations, shared rate limits, functional availability, Valkey data structures or
server-side scripts, Sentinel discovery, persistence, and rolling-upgrade policy. Those protocol
surfaces require separate transition design and atomicity review before being added to the store.

No deployment, replica count, worker configuration, health/readiness behavior, scheduler behavior,
rate-limit behavior, metric, chart, or desktop artifact changes as a result of this ADR.
