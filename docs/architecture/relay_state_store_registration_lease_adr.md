# ADR: registration and lease boundary for relay state

- **Status:** accepted foundation; not integrated
- **Date:** 2026-08-19
- **Issue:** [#1569](https://github.com/futuroptimist/token.place/issues/1569)

## Context and decision

Correctness-critical relay coordination currently lives in process-local globals. A typed
`RelayStateStore` boundary is needed before that state can safely move to a shared backend. This
first, deliberately small slice defines only compute-node registration, renewal, lookup/listing,
lease expiry, and explicit unregistration. It includes an atomic, lock-protected memory backend
but is not connected to `relay.py`; current routes and runtime behavior therefore remain unchanged.

The public API expresses lifecycle transitions rather than generic key/value access. Configuration
makes an environment/cluster namespace, schema version, authoritative lease TTL, compute-node
capacity, and node-identifier byte bound explicit. Stored registration and capability records are
frozen and contain only fields required by the current API-v1 scheduler. Capability validation
retains its current bounds: 64 model identifiers of at most 128 characters, the `8k-fast` and
`64k-full` tiers, positive token values capped at 1,000,000, and concurrency capped at 128.

Deadlines are UTC epoch values obtained from an injectable clock. Persisted records never contain
Python `time.monotonic()` values. Expiry occurs when `lease_expires_at_epoch <= now`. Registering an
existing live node renews its lease and capabilities but retains its original owner digest;
renewal is idempotent, may atomically replace validated capabilities, and never creates an unknown
node. Explicit removal is idempotent for an unknown or expired node. New nodes fail at the
configured capacity bound, while duplicate renewal remains possible at capacity. Each memory
operation holds one re-entrant lock, so registration, renewal, expiration, and removal cannot
expose duplicate or partially updated records.

Only a lowercase SHA-256 control-credential digest enters stored state. Raw control credentials,
relay private keys, prompts, messages, tool data, model output, ciphertext queues, and arbitrary
payload dictionaries are absent from the typed API. This narrow allowlist preserves the
relay-blind E2EE rule and prevents unbounded application payloads from becoming registration
metadata.

## Backend contract and consequences

The backend tests obtain stores through a factory. A future Valkey implementation will be added to
that factory and must pass the same deterministic lifecycle, bounds, defensive-read, clock, and
concurrency suite. This provides a backend-neutral behavioral baseline without choosing Valkey
keys, transactions, scripts, or discovery mechanisms prematurely.

Memory-only operation and a registration-only boundary keep this change reviewable and avoid a
partial runtime migration that could imply false horizontal-scaling guarantees. This PR is a
prerequisite only: it does **not** make the relay horizontally scalable and does not satisfy issue
#1569 by itself.

## Explicitly deferred

The following remain future design and implementation work:

- migration of runtime routes or rate-limit ownership lookups;
- terminal transitions (completion, failure, and cancellation), queues, claims/reclaim, responses,
  tombstones, and outcome deduplication;
- scheduler reservations and cursors, shared rate limits, and functional availability;
- Valkey data structures, transactions/server-side scripts, and failure semantics;
- Sentinel discovery and HA deployment topology;
- schema-compatible rolling-upgrade policy.

Those later slices must also address datastore availability and fail-closed behavior. No
availability endpoint, metrics, replica/chart changes, worker configuration, or deployment policy
is introduced here.
