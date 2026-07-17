# API v1 relay compute-control contract

`POST /api/v1/relay/requests/control` is an internal, relay-to-compute control
operation for a single non-streaming API v1 encrypted request. It is intentionally
narrow and does not expose request ciphertext, plaintext, client keys, cancel
proofs, or payload contents.

## Authentication and ownership

Callers must authenticate with the existing compute registration token when one
is configured. The body must identify the exact registered `server_public_key`,
`client_public_key`, and `request_id`. The relay only reports active in-flight or
cancellation tombstone state when the registered server owns that request. Other
registered servers receive `completed/unavailable` for requests they do not own;
unregistered or unauthenticated callers fail closed.

## Request body

```json
{
  "server_public_key": "registered compute node key",
  "client_public_key": "request client key",
  "request_id": "opaque request id",
  "acknowledge": false
}
```

Set `acknowledge` to `true` after observing a cancelled or expired status to
allow the relay to delete the short-lived compute-visible tombstone.

## Response body

The response status is one of:

- `active`
- `cancelled`
- `expired`
- `completed/unavailable`

Responses include `request_deadline_unix`, `request_ttl_seconds`, and a bounded
`next_poll_seconds` hint when the relay has authoritative deadline information.
A valid active poll renews only the in-flight accounting lease; it never extends
the absolute request deadline established when the relay admitted the request.
