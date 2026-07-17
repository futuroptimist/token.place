# API v1 relay compute-control contract

The relay exposes one narrow internal compute-control operation for work that has
already been dispatched to a registered API v1 compute node:

`POST /api/v1/relay/requests/control`

The route is for compute nodes only. It reuses the relay server-registration
authentication primitive (`X-Relay-Server-Token` when configured) and only returns
state for the exact registered `server_public_key` that owns the in-flight
`request_id` for the supplied `client_public_key`. A node that is unregistered,
unauthenticated, or polling a request owned by another node receives no usable
request state.

Request body:

```json
{
  "server_public_key": "registered compute-node public key",
  "client_public_key": "client public key from the encrypted envelope",
  "request_id": "request identifier from the encrypted envelope",
  "acknowledge": false
}
```

Response bodies are intentionally bounded and privacy-safe. They contain only:

- `status`: one of `active`, `cancelled`, `expired`, or `completed/unavailable`.
- `request_deadline_unix_ms` and `request_ttl_seconds` when a deadline is still
  known.
- `next_poll_seconds`, capped by the relay.

The route never returns ciphertext, client keys, cancel proofs, prompt contents,
responses, tool arguments, or payload bodies. A valid `active` poll refreshes only
the in-flight accounting lease so the relay does not evict a still-running
request as stale. It never extends the absolute request deadline established when
the relay admitted the request. Cancelled or expired in-flight requests leave a
short-lived, owner-visible tombstone until the owner acknowledges it or the
bounded tombstone TTL elapses.

API v1 remains non-streaming: clients still retrieve a final encrypted response
from `/api/v1/relay/responses/retrieve`, and late response submissions after
cancellation or expiry are rejected.
