# Authenticated inference: candidate protocol

Status: candidate specification for review; not implemented or qualified for use.
This document refines the [compute trust design](verified-compute-trust.md).
The [review and vector plan](authenticated-inference-review-plan.md) records the
blocking decisions and evidence required before implementation. Normative words
below describe the candidate contract, not guarantees of the current runtime.

## 1. Scope, policy and evidence boundary

The initial product policy accepts public operator bundles supplied by a user or
administrator and independently verified. Setup explains who receives plaintext
and records explicit trust. No project-maintained operator offering is required.
Arbitrary self-hosted relays remain supported; there is no mandatory central
account, relay allowlist or live centralized authorization lookup.

Offline verification is expiry-bound. It requires locally valid policy, signed
revocation status, persisted monotonic state and trustworthy time. There is no
guaranteed minimum disconnected interval. Changing relay URLs cannot grant trust.
Missing or expired authority stops verified dispatch and result acceptance.

API v1 remains non-streaming. Progress is authenticated telemetry, never partial
answer output. No API v2 or deprecated-route fallback is authorized. New clients
default to verified mode after setup; existing clients without a saved explicit
mode choice pause dispatch. Compatibility means explicitly selected encrypted
API v1 with a visible trusted-relay label; verification failure cannot select it.

The trusted client and chosen compute operator process plaintext. Signatures do
not establish correct inference, model provenance, hardware attestation or privacy
practices. This proposal makes no blanket forward-secrecy, traffic-analysis or
availability guarantee. Independently trusted client delivery and updates are
necessary: mutable verifier code supplied by an untrusted relay is insufficient.
Gabriel must encrypt within its trusted local client boundary before relay ingress.

Source inspection at token.place commit
`a42a58f824f21d5b4c51a22db838116899d49983` confirms that
[discovery/dispatch](../../api/v1/compute_provider.py) still uses relay-selected
compute keys and response correlation checks. The merged trust design is not a
runtime mitigation. This documentation neither closes those findings nor supplies
executed crypto vectors or interoperability evidence.

## 2. Profile and strict encoding

Candidate profile `tp-auth-inference-1` selects HPKE base mode with
DHKEM(X25519, HKDF-SHA256), HKDF-SHA256 and AES-256-GCM, plus separate Ed25519
signatures and JCS. Its exact `suite` object is
`{"aead":2,"kdf":1,"kem":32,"mode":0}`. No other values are accepted in this
profile. HPKE base mode does not authenticate the sender; the application
sign-then-encrypt composition below needs independent cryptographic review.

Every object has exactly the fields listed for its type. All fields are required
unless explicitly marked optional. Reject unknown fields, duplicates at any
nesting level, missing fields, type coercion, invalid UTF-8, a BOM, trailing data,
unpaired surrogates and non-JSON values. Duplicate detection occurs on decoded
property names before canonicalization; escaped aliases are still duplicates.
Do not normalize Unicode. Strings compare by their exact decoded scalar sequence.

| Notation | Exact candidate representation |
| --- | --- |
| `U` | JSON integer from 0 through 9007199254740991 inclusive; reject booleans, negative zero, fractional/exponent lexical forms and larger integers before numeric conversion. |
| `Time` | `U` seconds since Unix epoch, UTC. Validity intervals are half-open: `not_before <= now < expires_at`. |
| `Id` | ASCII string of 1 through 128 characters matching `[A-Za-z0-9][A-Za-z0-9._:-]*`; case-sensitive, no trimming. |
| `B(n)` | Unpadded RFC 4648 base64url of exactly `n` decoded bytes. Decode then re-encode and demand equality; reject whitespace, padding, alternate alphabet and nonzero unused bits. |
| `Bytes` | Same unique base64url representation, variable decoded length bounded by the applicable approved limit. |
| `Text` | JSON string; UTF-8 byte length bounded by the applicable approved limit. No implicit normalization. |
| `EdKey` | Object with `algorithm:"Ed25519"`, `key_id:B(32)`, `public_key:B(32)`. Pure Ed25519, not Ed25519ph or Ed25519ctx. |
| `XKey` | Object with `algorithm:"X25519"`, `key_id:B(32)`, `public_key:B(32)`. HPKE key encoding/validation follows RFC 9180. |
| `Scope` | Object with `nodes:Id[]`, `models:Id[]`, `profiles:Id[]`, `acceptance_domains:Id[]`. Arrays are nonempty, duplicate-free, ASCII-lexicographically sorted; exact membership, no wildcard syntax. |

`J(X)` means the UTF-8 bytes of RFC 8785 JCS for object `X`. All transmitted JSON
records, including outer wrappers, must already equal their canonical `J(X)`
bytes after strict parsing; reject alternative whitespace/key order/escaping.
The integer-only structural rule deliberately narrows general JCS numbers.

For application payloads, `JsonValue` permits null, boolean, `Text`, `U`, arrays
of `JsonValue`, and objects with `Text` property names and `JsonValue` values.
It has bounded depth, members, strings and total bytes. Negative or fractional
model options are not silently converted: the option registry below must define
a canonical decimal string for each such option. Decimal strings match
`-?(0|[1-9][0-9]*)(\.[0-9]*[1-9])?`, exclude `-0`, and have no exponent, plus sign
or trailing fractional zeros. Unrecognized option names or out-of-range values
are rejected. Choosing the exact supported option/result registries and their
runtime mappings is **blocking P02**, not permission to discard API fields.

Byte framing is defined without JSON string concatenation:
`F(label, bytes) = UTF8("token.place/auth-inference/1/") || ASCII(label) || 0x00 ||
uint32be(length(bytes)) || bytes`. Labels below are fixed literals, and lengths
count bytes. Inputs exceeding 4294967295 bytes are invalid regardless of policy.
`D(label, X) = B64url(SHA-256(F(label, J(X))))`.
A key's ID is `D("key-id", {"algorithm": algorithm, "public_key": public_key})`.
An enrolled `root_id` equals its Ed25519 key ID; never infer enrollment from a
matching key ID. Human fingerprint display is `sha256:` followed by 64 lowercase
hex digits of that same key-ID digest. Encoding is a candidate; the independent
comparison ceremony and recovery are blocking P04.

Every resource/lifetime limit named in this document is mandatory and finite.
A missing limit means this profile is unavailable, not unlimited operation.
**P03 blocks profile activation** until reviewed numeric values and rationale are
published for request/response bytes, strings, arrays, depth, chain/bundle sizes,
status count, progress rate/count, lifetimes, clock uncertainty and retention.
The concrete Id/U/key sizes above are candidate encoding choices, not security
approval of resource or lifetime bounds.

## 3. Signed public records and local enrollment

A signed object `S` has exactly `record:Object`, `signer_key_id:B(32)` and
`signature:B(64)`. The signature input is
`F("signature/" + record.type, J({"protected": H, "record": record,
"signer_key_id": signer_key_id}))`.
For public records `H` is JSON null; for encrypted messages it is the complete
protected header from section 4. The only permitted suffixes are `issuer`,
`descriptor`, `revocation`, `request`, `response`, `error`, `progress` and
`cancel`. The plus notation selects one fixed label, not caller-provided text.
The signature field is excluded from its own input. Verify the entire record
before using it. Exact Ed25519 acceptance behavior, including noncanonical and
small-order inputs, is a cross-runtime qualification blocker (P01/P09).

### 3.1 Imported bundle (not self-authenticating)

| Field | Type and constraint |
| --- | --- |
| `schema` | Literal `tp-operator-bundle/1`. |
| `operator_id` | `Id`; enrollment binds it to this root. |
| `label` | `Text`; display-only, not an identity check. |
| `root` | `EdKey`; fingerprint must be independently compared. |
| `scope` | `Scope`; imported scope only restricts local permission. |
| `expires_at` | `Time`; local policy may shorten, never silently extend it. |
| `revocation_urls` | Nonempty bounded array of distinct HTTPS URL strings. No userinfo or fragments; distribution hints, not authority. |
| `recovery_note` | `Text`; display-only independent recovery instructions, never executable configuration. |

Importing or downloading a bundle alone grants no trust. Local enrollment records
root fingerprint, approved scope, provenance, explicit consent or managed-policy
source, expiry and policy generation. The persistent trust-store schema, managed
policy precedence, rollback protection and independent delivery are P04/P05 gates.
Cancellation/fingerprint mismatch leaves the profile unenrolled. Ordinary users
need no operator private key. URLs fetched from a bundle cannot run code, import
roots or replace the trusted client. Safe retrieval/redirect rules are P04.

### 3.2 Issuer delegation

Candidate delegation depth is at most one issuer below the enrolled root. An
empty chain means direct root issuance; otherwise `issuer_chain` contains exactly
one signed `issuer` record, signed by the root. Cycles, extra links or duplicate
keys fail. Issuers cannot delegate further. Increasing depth requires profile
review, not accepting an undocumented longer chain.

| Field | Type and constraint |
| --- | --- |
| `type`, `schema` | Literals `issuer`, `1` (integer). |
| `root_id`, `operator_id` | `B(32)`, `Id`; match enrolled policy. |
| `issuer_key_id` | `B(32)`; equals enrolled root key and wrapper signer. |
| `subject_key` | `EdKey`; distinct from root and node signing/encryption keys. |
| `scope` | `Scope`; every list is a subset of enrolled scope. |
| `can_delegate` | Literal `false`. |
| `not_before`, `expires_at` | `Time`; nonempty interval, constrained by policy and issuer lifetime limit. |
| `generation` | `U`; monotonic for this issuer identity. |

### 3.3 Compute descriptor

| Field | Type and constraint |
| --- | --- |
| `type`, `schema` | Literals `descriptor`, `1`. |
| `root_id`, `operator_id`, `node_id` | `B(32)`, `Id`, `Id`; enrolled and within all scopes. |
| `issuer_key_id` | `B(32)`; equals wrapper signer and direct root or delegated subject. |
| `issuer_chain` | Zero or one signed issuer records as above. |
| `encryption_key`, `signing_key` | `XKey`, `EdKey`; distinct from each other, root and issuer keys; no key-purpose reuse or encryption-to-signing conversion. |
| `acceptance_domain` | `Id`; stable compute-controlled replay authority, covered by scope. |
| `scope` | `Scope`; includes this node/domain and selected model/profile; subset of all ancestors. |
| `offered_profiles` | Sorted, unique nonempty `Id[]`; each within `scope.profiles`. This profile must be present. |
| `not_before`, `expires_at` | `Time`; nonempty interval wholly inside issuer and enrollment intervals. |
| `generation` | `U`; monotonic for `(root_id, operator_id, node_id)`. |
| `min_revocation_epoch` | `U`; snapshot used at acceptance must meet this floor. |

`descriptor_digest = D("descriptor", S)` covers the complete signed descriptor
including issuer chain and offered capabilities. A request binds this digest and
its exact selected profile. Stripped capabilities cannot cause downgrade. Records
with equal identity/generation but different canonical bytes quarantine that scope;
older generations cannot replace discovery state. An already bound in-flight
descriptor survives ordinary overlap rotation only while currently authorized.
Scope lists and validity intervals can only narrow down the chain.

### 3.4 Revocation snapshot

| Field | Type and constraint |
| --- | --- |
| `type`, `schema` | Literals `revocation`, `1`. |
| `root_id`, `operator_id` | `B(32)`, `Id`; one complete root/operator namespace. |
| `epoch` | `U`; monotonically increasing within that namespace. |
| `not_before`, `expires_at` | `Time`; nonempty interval subject to freshness/lifetime limits. |
| `revoked_key_ids` | Sorted unique `B(32)[]`; issuer or node keys, sorted by encoded ASCII. |
| `revoked_descriptor_digests` | Sorted unique `B(32)[]`, by encoded ASCII. |

The wrapper signer is the enrolled root. This candidate keeps snapshot authority
root-only and requires complete cumulative revocation sets, not partial deltas.
A newer snapshot must include all previously persisted revocations; omitted entries
cannot restore a key. Root signing custody/cadence and any future separately
constrained status issuer are blocking P05. A compromised root cannot authorize its
own trustworthy recovery. Independent re-enrollment is required.

## 4. Encrypted records and composition

### 4.1 Outer envelope and protected header

The encrypted outer object has exactly `protected:H`, `enc:B(32)` and
`ciphertext:Bytes`. Ciphertext includes the AEAD tag and must be at least 16 bytes.
Reject a plaintext body, plaintext-derived digest, legacy `cipherkey`/`iv`, model
output or any extra field. API v1 transport control/admission tokens remain
separate bounded transport fields, never signatures or compute identity proof.
The exact route adapter/transport allowlist is blocking P02/P10; this envelope
alone does not authorize adding endpoints or passing existing arbitrary metadata.

| Header field | Type and constraint |
| --- | --- |
| `profile`, `suite` | Literal `tp-auth-inference-1` and exact suite from section 2. |
| `direction` | Literal `client-to-compute` or `compute-to-client`. |
| `root_id`, `operator_id`, `node_id` | `B(32)`, `Id`, `Id`; match selected descriptor. |
| `descriptor_digest` | `B(32)`; public descriptor digest, not request/body digest. |
| `recipient_key_id` | `B(32)`; selected compute encryption key or bound client response key. |
| `route_id` | `B(32)`; fresh independent random bytes for this attempt, not a plaintext hash. |
| `expires_at` | `Time`; exactly the originating request's expiry, never a relay extension. |

Relay visibility is limited to this header, encapsulation, ciphertext lengths,
transport timing, destinations and separately bounded admission/control data.
Model/options, continuity key, request nonce and request digest stay encrypted.
Selection model/tier hints in current clients need an explicit privacy and adapter
review (P02/P10); they are not implicitly allowed by this candidate envelope.

Each new encrypted message uses a fresh RFC 9180 base-mode sender context and one
`Seal`, at HPKE sequence zero. Never reuse an encapsulation/context to encrypt a
second plaintext. HPKE derives its AEAD nonce; the request nonce is a separate
32-byte random replay identifier and never an AEAD nonce. No custom AES nonce API
or exporter use is part of this profile. Exact transport retransmission resends
stored bytes rather than performing another encryption with a reused context.

Set `info = F("hpke-info", J(H))` and
`aad = F("hpke-aad", J(H))`. Encrypt `J(S)`, the signed object from section 3 with
`protected = H` in its signature input. Recipient keys come from validated local
state, never an unverified relay key. After decryption, perform strict parsing,
signature verification, record/header matching and current authorization checks
before any content or progress is consumed. A valid HPKE tag alone is insufficient.

The inner signature binds sender continuity/authorization and the complete result
or request to the exact recipient context; encrypting it keeps request digests
private. Public descriptors bind compute keys and offered profiles. Fixed labels
separate signing and hashing purposes and HPKE context. These are composition
arguments for review, not a proof; P01 covers substitution, reflection, unknown-key
share, cross-protocol use and decryption/error oracles.

### 4.2 Request record

| Field | Type and constraint |
| --- | --- |
| `type`, `schema`, `profile`, `suite`, `mode` | Literals `request`, `1`, `tp-auth-inference-1`, exact suite, `verified`. |
| `root_id`, `operator_id`, `node_id`, `descriptor_digest` | Exact header/descriptor bindings. |
| `acceptance_domain` | `Id`; exactly descriptor value. |
| `compute_encryption_key_id`, `compute_signing_key_id` | `B(32)` each; exactly descriptor keys. |
| `client_response_key`, `client_continuity_key` | Fresh per-attempt `XKey`, `EdKey`; independent generation and purposes. |
| `nonce`, `route_id` | Independent fresh `B(32)` each; route ID equals header. |
| `created_at`, `expires_at` | `Time`; `created_at < expires_at`, expiry equals header; duration within request limit. |
| `model`, `context_tier` | `Id` each; explicitly supported registry entries, model within authorized scope. |
| `options` | Object from exact approved model-option registry; unknown entries fail (P02). |
| `body` | Complete API v1 request `JsonValue` object under the approved schema (P02); no omitted messages/tool arguments or unsigned semantic fields. |

The wrapper signer equals `client_continuity_key.key_id`. Its verification proves
control of this request key, not an account or admission entitlement. Compute
abuse limits/client authorization remain separate operator policy. Header recipient
is `compute_encryption_key_id`; direction is `client-to-compute`. All repeated
model/options/tier fields in the API body must match their record counterparts;
profile-to-API mapping must specify absence/default behavior before activation.

`request_digest = D("request", request_record)` covers the whole record, excluding
only the signature wrapper. It is stored at client and authoritative compute and
never exported to relay state. Replay identity is the tuple
`(root_id, operator_id, acceptance_domain, client_continuity_key.key_id, nonce)`.
Node/key rotation cannot create a second authority for this tuple in the domain.

### 4.3 Shared response, error and progress binding

Each compute-to-client record has the following fields plus exactly its variant
fields below. The wrapper signer is the request's `compute_signing_key_id`, using
the original descriptor. Rotation never silently changes the responder key.

| Field | Type and constraint |
| --- | --- |
| `type`, `schema`, `profile`, `suite`, `mode` | Type from table below; `1`, selected profile/suite, `verified`. |
| `root_id`, `operator_id`, `node_id`, `descriptor_digest`, `acceptance_domain` | Exact stored request values. |
| `compute_encryption_key_id`, `compute_signing_key_id` | Exact stored request values. |
| `client_response_key_id`, `client_continuity_key_id` | `B(32)` each; exact stored request key IDs. |
| `request_digest`, `nonce`, `route_id` | `B(32)` each; exact stored request bindings. |
| `created_at`, `expires_at` | `Time`; creation within original request interval; expiry exactly original request expiry. |

Header direction is `compute-to-client`, recipient is the stored response key,
and every copied header field must match. All variants are encrypted to that key.
Recheck authorization at result acceptance; validity when work began is insufficient.

| Variant | Additional fields and behavior |
| --- | --- |
| `response` | `result:Object` containing the complete approved API v1 response schema (P02). Terminal; no error or progress field. |
| `error` | `error:{code:Id,message:Text}`. Terminal authenticated compute failure; `code` must come from the approved finite error registry (P02); no retry directive can grant fresh execution. |
| `progress` | `sequence:U` starting at 1, `phase` one of `accepted`, `queued`, `running`; no output text or free-form message. Monotonically increasing per replay identity across worker changes, bounded by approved count/rate. It neither completes work nor extends expiry. |

An unbound/undecryptable request cannot receive a trustworthy request-bound compute
error. Unauthenticated relay/network errors are availability observations only and
cannot complete a verified attempt as a compute outcome. Diagnostics must not log
raw content, keys, signatures, ciphertext or plaintext-derived digests.
An authenticated terminal rejection also requires retained terminal/tombstone
state. If that cannot be committed, report transport unavailability only; do not
claim a durable terminal compute outcome or permission for a new attempt.

### 4.4 Cancellation

A `cancel` record contains exactly `type:"cancel"`, `schema:1`, `profile`, `suite`,
`mode:"verified"`, `root_id`, `operator_id`, `node_id`, `descriptor_digest`,
`acceptance_domain`, `compute_encryption_key_id`, `compute_signing_key_id`,
`client_response_key_id`, `client_continuity_key_id`, `request_digest`, `nonce`,
`route_id`, `created_at`, `expires_at`, `client_continuity_key:EdKey` and
`client_response_key:XKey`. The full keys match the two key IDs; all other
types/bindings equal section 4.3. It is signed by the original continuity key and
encrypted to the original compute key using
`client-to-compute`. It does not alter the accepted request digest or authorize
execution. There is no new relay endpoint implied by this record (P10).

Local cancellation first atomically closes client acceptance; late output is never
rendered. Authenticated compute cancellation can fence pending work but does not
prove that physical execution stopped. The race with success is serialized by the
acceptance store: one terminal outcome wins. Cancellation before request acceptance
validates the supplied continuity key/signature, descriptor, claimed request
interval and current authorization before reserving a tombstone for that identity
and request digest through the replay window. The full key permits validation
without an already stored request. It cannot grant request admission; a delayed
original request must not acquire authority. If capacity cannot retain that tombstone,
cancellation cannot claim compute prevention; the client remains locally closed.

## 5. Lifecycle and offline decisions

Treat trustworthy time as an interval `[earliest_now, latest_now]`, not a relay
clock. Accept a validity interval only if `not_before <= earliest_now` and
`latest_now < expires_at`. For every encrypted record, use `created_at` as its own
lower bound, in addition to the original request interval wherever that request is
known.
Clock skew/uncertainty policies may narrow acceptance but cannot extend signed
expiry. Time evidence source, maximum uncertainty, suspend/resume and offline reboot
behavior require P03/P05 approval. Missing reliable time means no verified use.

Effective authority ends at the earliest expiry of local policy/bundle, issuer,
descriptor, request and applicable revocation status. Both client and compute
recheck relevant authority at admission and output acceptance/publication. An
unexpired descriptor cannot bypass a stale snapshot. A suppressed update remains
an unavoidable risk within the approved freshness window; a stale snapshot stops
use. Neither lifetime length nor a locally cached bundle promises offline uptime.

Apply epoch/generation transition rules only after verifying the candidate
record signature, enrolled root, scope and structural constraints. Unauthenticated
data cannot advance monotonic state or establish an authenticated conflict.

| Event | Required candidate transition |
| --- | --- |
| Bundle unconfirmed, fingerprint mismatch or setup cancelled | Remain unenrolled; do not dispatch. |
| Valid first snapshot after independent enrollment | Persist complete snapshot, epoch, digest and rollback guard atomically before authorizing use. |
| Lower snapshot epoch | Reject; never replace newer policy or reauthorize work. |
| Equal epoch, identical canonical snapshot | Idempotent; no extension of expiry/freshness. |
| Equal epoch, different canonical snapshot | Quarantine root/operator scope; block dispatch and output acceptance until independent resolution. |
| Higher epoch with invalid signature/scope/interval or omitted prior revocations | Reject without advancing accepted epoch. Do not renew old snapshot freshness. |
| Higher valid epoch | Atomically persist snapshot/high-water state and mark affected in-flight attempts invalid before further acceptance. |
| Snapshot update interrupted | Recover one complete committed state or block; never combine epoch from one update with contents of another. |
| Expiry, stale/missing status, clock rollback/excess uncertainty | Suspend verified use; accepted compute work confers no right to accept its later output. |
| In-flight issuer/key/descriptor revoked | Fence new work/publication where possible; client invalidates affected attempts, including late completion. Physical stopping is a separate obligation. |
| Planned key rotation | Revalidate new descriptors for new attempts; original in-flight bindings remain fixed and subject to current revocation. |
| Store loss, rollback, reinstallation or uncertain monotonic state | Block; independent recovery must restore trustworthy high-water/deny state before use. |
| Root compromise/replacement | Independent recovery; signatures from the compromised root alone cannot restore trust. |

The validation and acceptance decision must observe one policy generation under
an atomic guard. If a newer snapshot or local cancellation commits before result
acceptance, the latter must see it. A revocation arriving after an already accepted
result cannot erase plaintext already consumed. P05/P06 must specify this ordering
across threads, processes and replicas and demonstrate crash-safe recovery.
The root/operator-wide snapshot is the candidate coordination boundary across its
issuers; fragmented issuer snapshots are unsupported pending a new reviewed rule.

## 6. Replay, retries and durable authority

The authoritative acceptance domain belongs to compute, not to relay caches.
Before acceptance, validate the complete record/signature/current authorization,
resource bounds and separate admission policy. Atomically reserve replay identity,
complete request digest, expiry, capacity and initial state before dispatch.
Same identity with different request digest is always a conflict. Exact retry may
retrieve retained status/result only; it never authorizes another inference.
Retransmission must preserve signed/header bindings and use stored envelope bytes.

| State/event | Authority and permitted next state |
| --- | --- |
| Absent, valid new request | Atomic durable reservation to `accepted`; failure has no dispatch permission. |
| `accepted`, worker claim | Commit one monotonically fenced worker grant to `running` before dispatch; persist the grant. |
| Duplicate while `accepted`/`running` | Compare digest; return retained authenticated status if allowed, otherwise bounded unavailability; never redispatch. |
| Crash after grant, before/after physical execution | If execution cannot be proven absent, move to `outcome_unknown`; do not re-grant automatically. |
| `running`, worker completes | Compare current fence, digest, policy generation and nonterminal state; atomically persist terminal result/error before reporting completion. |
| Stale worker or conflicting terminal write | Reject. No replacement result, repeated side effects or second terminal client acceptance. |
| `terminal`, exact retry | Return the same retained signed/encrypted terminal bytes only if currently authorized and unexpired; otherwise refuse. |
| Authorized cancel wins before terminal | Persist `cancelled`/tombstone and invalidate worker authority; physical worker completion remains possible but cannot publish. |
| `outcome_unknown` | Retain identity/fence; report unavailability honestly. Only an independently justified recovery proof may establish a safe next state. |
| Request expires | Reject retry/output; retain tombstone through all reviewed replay/recovery windows. |
| Capacity exhausted | Reject before acceptance. Do not evict live replay authority to admit work. |
| Store lost, partitioned, restored backwards or ownership uncertain | Stop new acceptance and unsafe publication until recovery establishes unique durable authority. |

Client states are `outstanding`, `accepted_terminal`, `cancelled`, `expired` and
`invalidated`. Only `outstanding` may atomically accept one verified terminal
record; all others refuse progress/output. Reordered or repeated progress with
`sequence <= highest_sequence` is discarded without advancing state. Progress
sequence persistence and terminal acceptance share the request's storage/locking
contract. Loss of client outstanding-request state cannot recreate acceptance.

Exactly-once inference is not promised by a durable database. Dispatch and an
external inference engine do not share a database transaction. P06/P07 must trace
crashes before/after reservation, grant, dispatch, execution, terminal persistence
and response delivery. Reissuing a worker grant is safe only with evidence that
prior authority cannot execute or publish; fencing a write alone cannot undo an
inference. Replica failover, backups, cleanup and tombstone retention need the same
proof. Expiry of the stored request alone is not a cleanup proof: a continuity
key holder could sign a different request with the same tuple and a later expiry.
No tombstone may be removed while that tuple could ever be admissible again.
P06/P07 must define an enforceable key/domain retirement fence and its durable
recovery rules before finite cleanup is enabled; absent that proof, retain state
and reject at capacity. Late writes cannot resurrect a terminal, revoked or
cancelled identity.

Operator failover creates a visibly separate attempt with fresh nonce, route ID
and client keys, after validating the new allowed operator. It must disclose
possible prior execution/cost and cannot relabel an unknown attempt as never run.
No relay timeout or changed node/key identity silently supplies retry authority.

Storage candidates remain unselected. SQLite may suit a deliberately single-host
domain with reviewed filesystem/durability settings; WAL is not a cross-host
network-filesystem solution. PostgreSQL may suit shared transactional authority;
serialization abort/retry, failover durability and worker fencing still need proof.
Neither backend, nor an in-memory/relay cache, makes execution exactly once.

## 7. Blocking boundary and references

Before activation, every blocking row P01-P10 in the
[review plan](authenticated-inference-review-plan.md#2-blocking-decision-ledger)
needs a named owner, resolved contract and acceptance evidence. No runtime may
claim this candidate is a qualified profile while any row is unresolved. Merging
these docs does not authorize vectors, adapters, enrollment, staging or rollout.

- [RFC 9180: HPKE](https://www.rfc-editor.org/rfc/rfc9180), especially mode/context/key validation and application requirements.
- [RFC 8032: Ed25519](https://www.rfc-editor.org/rfc/rfc8032).
- [RFC 8785: JCS](https://www.rfc-editor.org/rfc/rfc8785).
- [RFC 4648: binary encodings](https://www.rfc-editor.org/rfc/rfc4648).
- [SQLite WAL](https://sqlite.org/wal.html) and [atomic commit](https://sqlite.org/atomiccommit.html).
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html).
