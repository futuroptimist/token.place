# API v1 compute-control request status contract

The relay exposes a narrow internal compute-control operation at
`POST /api/v1/relay/requests/control` for API v1 compute nodes that already own
an in-flight encrypted request. The route is not a user-facing API and does not
change the frozen non-streaming API v1 final-response contract.

## Authentication and ownership

Compute-control requests use the same relay server-registration token validation
as API v1 register, unregister, poll, and response submission routes. A control
poll must include the registered `server_public_key` and a `request_id`; the
relay returns active or cancellation state only when that exact registered server
currently owns the request or has an unacknowledged owner-visible tombstone for
that request.

Server registration heartbeat state remains separate from per-request in-flight
ownership. A valid control poll may renew the per-request in-flight accounting
lease, but it never extends the absolute request deadline established at relay
admission.

## Response body

The response is intentionally bounded and privacy-safe. It returns only:

- `status`: one of `active`, `cancelled`, `expired`, or `completed/unavailable`.
- `deadline_unix_ms` / `request_deadline_unix_ms` when known.
- `remaining_ttl_seconds` when a deadline is known.
- `next_poll_seconds`, a bounded polling hint.

The relay must not include ciphertext, plaintext prompts, client keys, cancel
proofs, encrypted payload bodies, tool arguments, or model output in this
contract, logs, or metrics.

## Tombstone acknowledgement

When a requester cancels an in-flight request, the relay removes the active
in-flight owner entry and keeps a short-lived tombstone visible only to the
owning compute server. The compute server may send `acknowledge: true` to clean
up that tombstone after observing the cancellation. Tombstones also expire after
a bounded relay-side TTL.
