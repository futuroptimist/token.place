# API v1 relay compute-control contract

The API v1 relay compute-control operation is a narrow, internal endpoint for a registered compute node to poll state for a request it already owns. It is relay-side foundation for cancellation visibility only; desktop worker termination is intentionally out of scope until the follow-up desktop cancellation work lands.

## Route

`POST /api/v1/relay/servers/control`

Request body:

```json
{
  "server_public_key": "registered compute node public key",
  "request_id": "opaque relay request id",
  "control_credential": "owner-bound opaque credential from initial register response",
  "acknowledge": false
}
```

`acknowledge` may be set to `true` after the compute node has observed a terminal control state. The relay then removes the short-lived compute-visible tombstone for that exact owner/request pair.

## Authentication and ownership

The route reuses the compute-node registration control-plane authentication (`X-Relay-Server-Token` when the relay is configured with registration tokens) and also requires an exact-owner `control_credential` proof. Registration returns this opaque credential only when first created; the relay stores only its digest bound to the exact `server_public_key`, and re-registration does not disclose or rotate an existing credential. This owner proof is required even when shared registration tokens are disabled.

A successful active or terminal state is visible only to the exact owner credential bound to the request. Polls for another server's request fail closed as `completed/unavailable`; they do not reveal cancellation state, ciphertext, client keys, cancel proofs, prompts, payloads, or model output.


## Exact-owner unregister

API v1 compute-node unregister uses the same exact-owner model as control polling. `POST /api/v1/relay/servers/unregister` requires the shared registration token when configured **and** the `control_credential` returned by that node's registration response while the node is live. Token-only, missing, wrong, or unsigned owner proof returns `403` without mutating the live registration, queues, in-flight request state, or owner-bound tombstones. Unregister remains idempotent for already-absent nodes and returns `removed: false` after the shared-token boundary succeeds.

The legacy `/unregister` alias is retained only for old relay compatibility. When its live target is an API v1 registration, the relay applies the same exact-owner credential check atomically with detaching that exact registration generation. Genuine legacy registrations that do not carry the API v1 marker keep token-only behavior. Modern API v1 clients send the matching owner credential to both the API v1 route and, when falling back to the same relay's legacy alias after a `404`, the legacy route. Old relays that never issued a credential keep their existing fallback behavior.

## Response states

Responses are JSON and intentionally bounded to control state:

- `active`: the relay still considers the request in flight for this server.
- `cancelled`: the client/requester cancellation won, including preserved `client_timeout` reasons for client-facing retrieval.
- `expired`: the authoritative relay deadline elapsed.
- `completed/unavailable`: the relay has no owner-visible active or terminal tombstone for this server/request pair.

The response may include `request_ttl_seconds` and `request_deadline_remaining_seconds`. These are relative values for backward compatibility and privacy; compute nodes must not treat a lease renewal as an extension of the absolute request deadline. `next_poll_seconds` is a bounded positive hint only.

## Deadline and lease behavior

The relay establishes one authoritative request deadline when a request is admitted to `/api/v1/relay/requests`. The default is compatible with the browser's existing roughly 300-second cancellation behavior and is configurable via relay environment variables while remaining bounded. The deadline is stored with queued and in-flight request state, returned in admission metadata, and dispatched with the encrypted request envelope as relative TTL metadata.

A valid active control poll may renew only the in-flight accounting lease for the owning compute node. It never extends the absolute request deadline. Late result/error submissions after cancellation or deadline expiry remain rejected.

## Privacy-safe observability

Metrics count bounded control request states (`active`, `cancelled`, `expired`, `acknowledged`, `completed_unavailable`) plus a separate lease-renewal counter, without request ids, ciphertext, client keys, cancel proofs, prompts, responses, or payload contents.
