# K061: independently verified compute identity

Status: proposed design, requested by Daniel; no runtime changes or deployment.
K061 remains the kanban tracking item; the coordinating task owns its board updates.
K069 owns the coordinated authenticated-envelope migration. Neither is implemented
by this document. This design is separate from staging-shadow and relay-state ADR work.

## Problem and evidence

The current encrypted path protects payloads from an honest relay that forwards the
selected compute key and envelopes correctly. It does not independently establish
who owns that key. No live compromise has been observed; this is source analysis,
not an incident report or a result from probing deployed services.

Evidence was checked against token.place main
`843bfff4cd380200c37fc9b7eda0275d561baad7` (#1910), which includes #1916, and DSPACE
main `97ab09f13fb098de928a878bf1fe9b8d13032cb5`:

| Source | Current behavior and limit |
| --- | --- |
| [Python provider discovery and dispatch](https://github.com/futuroptimist/token.place/blob/843bfff4cd380200c37fc9b7eda0275d561baad7/api/v1/compute_provider.py#L317-L415) | Takes `server_public_key` from `/api/v1/relay/servers/next`, checks that it is nonempty, and encrypts the request to it. No independently provisioned operator identity is checked there. |
| [Python response handling](https://github.com/futuroptimist/token.place/blob/843bfff4cd380200c37fc9b7eda0275d561baad7/api/v1/compute_provider.py#L531-L560) | Decrypts and checks protocol, request ID and client key. These are correlation checks, not compute sender authentication. |
| [DSPACE selection](https://github.com/democratizedspace/dspace/blob/97ab09f13fb098de928a878bf1fe9b8d13032cb5/frontend/src/utils/tokenPlace.js#L717-L766) and [dispatch](https://github.com/democratizedspace/dspace/blob/97ab09f13fb098de928a878bf1fe9b8d13032cb5/frontend/src/utils/tokenPlace.js#L1007-L1078) | Normalizes the relay-returned public key and encrypts to it. Tier/model checks do not establish operator identity. |
| [DSPACE response validation](https://github.com/democratizedspace/dspace/blob/97ab09f13fb098de928a878bf1fe9b8d13032cb5/frontend/src/utils/tokenPlace.js#L901-L925) | Checks protocol/version, request ID, client key and response presence; no compute signature is verified. |
| [Current CryptoManager](https://github.com/futuroptimist/token.place/blob/843bfff4cd380200c37fc9b7eda0275d561baad7/utils/crypto/crypto_manager.py#L183-L240) | Public-key encryption is callable with a recipient public key alone; the API v1 compatibility path requests PKCS#1 v1.5. This is not the proposed authenticated protocol. |
| [Destination-bound admission headers](https://github.com/futuroptimist/token.place/blob/843bfff4cd380200c37fc9b7eda0275d561baad7/utils/networking/relay_client.py#L1583-L1587), [credential validation](../../utils/networking/relay_credentials.py), [HTTP redirect handling](../../utils/networking/http_requests_compat.py) | #1916 binds registration credentials to the intended relay destination and prevents credential-bearing redirects. It does not give clients an independent compute trust root. |
| [Disabled plaintext routes](https://github.com/futuroptimist/token.place/blob/843bfff4cd380200c37fc9b7eda0275d561baad7/relay.py#L2873-L2908) | Historical `/relay/api/v1/chat/completions` and `/relay/api/v1/source` return 503. They are not evidence of an active plaintext bypass. |

A malicious relay can return its own encryption key as the selected compute key.
The client then encrypts the prompt to that relay-controlled key. The relay could
read it and optionally re-encrypt it to a real compute node, concealing substitution.
Separately, anyone with the public client key can construct an encrypted response;
matching relay-visible request identifiers does not prove compute authorship.
Even an AEAD envelope alone cannot identify the intended compute operator if the
relay selected the underlying key without independent verification.

HTTPS authenticates the chosen transport endpoint and protects that hop; it does
not make that endpoint honest. Registry admission/control credentials constrain
participants under an honest relay, but a malicious registry/relay can lie to the
client. #1916 addresses credential destination leakage, not this identity problem.
Keep all existing ciphertext-only guards and disabled plaintext routes intact.

## Decision and threat boundaries

Daniel's selected preference is **verified compute by default for new clients**:
users choose operator trust roots provisioned independently of relay discovery.
Relay selection remains useful for availability, but cannot grant identity trust.
Any self-hosted relay, including `https://example.com`, remains supported. There is
no token.place-only hostname allowlist and no mandatory central operator authority.

A trusted client verifies operator delegation, compute keys and the complete
request/response binding before releasing a prompt or accepting output. This aims
to resist relay key substitution, forged output, tampering, replay and downgrade.
It does not prevent relay denial of service, traffic analysis, delayed delivery,
or observation of permitted routing metadata. The chosen compute operator still
receives plaintext and can retain it or return misleading model output. Signatures
prove key possession/authorization, not hardware attestation, correct inference,
model provenance, or an operator's privacy practices. Compromised trusted roots,
compute endpoints and client devices remain outside that guarantee.

Client delivery is part of the trust boundary. A malicious relay serving mutable
JavaScript can replace the verifier, trust settings and UI before encryption.
Verified mode therefore requires an independently trusted client distribution or
trusted web origin distinct from the untrusted relay, with a reviewed update chain.
TLS or a checksum served by the same malicious origin is insufficient. Self-hosted
clients are supported; trusting their delivery is an explicit user responsibility.
The relay landing page cannot claim malicious-relay resistance merely by adding JS
signature checks to code the relay can replace.

## Bootstrap and operator lifecycle

1. Install/open a trusted client. Enter the relay URL separately from the operator
   choice; changing a URL must not reset or expand trust.
2. Import an operator trust bundle through an independent channel: an administrator
   policy, verified package, or operator contact with an independently compared
   fingerprint/QR. The UI shows operator label, root fingerprint, source, scope and
   expiration. A friendly name or bundle fetched only from the relay is not proof.
3. Confirm the operator(s) allowed to receive prompts, with per-profile policy for
   models and minimum protocol suite. CLI/Python clients use the same explicit
   trust-store file/policy, not an implicit browser-only trust decision.
4. Discovery returns signed compute descriptors. The client checks the chain to an
   already trusted root and policy before encrypting. No trusted eligible operator
   means no dispatch, with a clear setup/recovery message.

An operator root signs short-lived compute descriptors binding operator ID, node
ID, distinct encryption and signing public keys, key IDs, allowed protocol suites,
validity times, generation/epoch and delegation scope. A self-signature by a newly
advertised key is not independent identity. Keep roots offline where practical;
use constrained issuing keys if operationally necessary and bound chain depth.

Planned rotation uses signed successor descriptors and bounded overlap. Pin trust
roots rather than individual short-lived compute keys; retain the selected key
identity for each in-flight request. Root replacement requires old-root-authorized
continuity plus local policy, or independent re-provisioning when compromise is
suspected. Never accept a relay-provided replacement root automatically.

Publish signed revocation snapshots with monotonic epochs and expiry through an
independently configured operator channel; relay mirrors are untrusted caches.
Persist the highest accepted epoch, reject rollback, enforce freshness using a
trusted clock with a specified skew bound, and fail closed once cached status
expires. A relay can suppress updates, so revocation has a bounded freshness window,
not instantaneous effect. Recheck validity/revocation before accepting a response;
revoked in-flight work fails closed. Offline clients may use unexpired cached policy
only. Clearing state/reinstall requires bootstrap again and must not erase rollback
protection silently. Root compromise requires independent recovery, not signatures
from the compromised root alone.

## Protocol direction and K069 contract

Use established schemes through maintained libraries. The preferred review profile
is [HPKE (RFC 9180)](https://www.rfc-editor.org/rfc/rfc9180) with
DHKEM(X25519, HKDF-SHA256), HKDF-SHA256 and AES-256-GCM, plus separate
[Ed25519 signatures (RFC 8032)](https://www.rfc-editor.org/rfc/rfc8032) for operator
certification and transcript authentication. HPKE base mode alone is not sender
authentication. K069 and security review must approve the composition, libraries
and cross-language vectors before implementation; this is not a new deployed suite.
Do not hand-roll cryptographic primitives or reuse RSA encryption keys for signing.

Define one versioned wire profile jointly with K069. Specify exact signed bytes,
domain separation for descriptors/requests/responses/progress, algorithm IDs,
length bounds and strict parsing. [JCS (RFC 8785)](https://www.rfc-editor.org/rfc/rfc8785)
is the proposed JSON canonicalization: reject duplicate keys and out-of-range
numbers, encode binary fields unambiguously, and test equivalent representations.
No informal JSON string concatenation or signature over only the payload text.

Proposed transcript requirements (field names are design concepts, not an API):

- Request: bind protocol/suite, trust mode, operator/node and both compute key IDs,
  fresh unpredictable request nonce/ID, client ephemeral signing and response
  encryption keys, creation/expiry, requested model/options and full request body.
  The ephemeral client key signs the request transcript inside encryption; this
  authenticates continuity of that request, not a real-world user account. Bind the
  recipient descriptor digest and context in HPKE info/AAD. Compute verifies all
  bindings before inference and rejects changed or expired transcripts.
- Response: compute's certified signing key signs the response body/error plus the
  exact request transcript digest, request nonce, both endpoint identities/key IDs,
  suite and expiry. Encrypt the signed response to the bound client response key.
  Client verifies authorization, signature and its stored request binding before
  rendering, returning API data, or executing tools. An error needs the same origin
  authentication as successful output; an unauthenticated relay error is only a
  transport/availability failure.
- Keep plaintext bodies and plaintext-derived request digests inside encryption;
  expose only bounded routing metadata. Bind security-relevant outer copies to the
  authenticated transcript and reject discrepancies. Relay reservation/control
  credentials remain separate; they are not operator certificates.
- Compute maintains bounded replay state for accepted client-key/nonce pairs through
  expiry, with an explicit restart/persistence and idempotent retry policy. Reject
  replay after expiry; never repeat inference merely because relay state reset.
  Client accepts at most one terminal response per outstanding request. Progress
  uses the same authenticated request binding plus monotonic sequence numbers;
  unverified progress is ignored and cannot complete or extend a request.
- Pin a minimum suite/version in local policy. Sign offered/selected capabilities
  and bind the selection to the transcript. Stripping advertisements or presenting
  legacy CBC/PKCS#1 v1.5 data must fail in verified mode, never trigger fallback.
  Failover creates a fresh attempt/nonce and re-verifies the new operator; retries
  must remain within the user's allowed operator set.

K061 owns identity/bootstrap/policy; K069 owns the authenticated envelope and
cross-language migration. Neither signed discovery alone nor AEAD alone closes
both gaps. API v1 remains non-streaming; an envelope revision does not enable API
v2 or deprecated relay routes. Forward secrecy is a separate review question:
static recipient keys, even with fresh per-message secrets, do not justify a blanket
forward-secrecy claim after recipient private-key compromise.

## Failure behavior and compatibility

| Mode | Admission and user-visible behavior |
| --- | --- |
| Verified compute (new-client default) | Requires independent roots, fresh authorization and the approved authenticated suite. Unknown/expired/revoked keys, invalid signatures, replay, mismatched transcript or unsupported suites stop dispatch/acceptance. Show a bounded diagnostic without prompts or decrypted output. |
| Trusted-relay compatibility | Explicit per-profile opt-in for an existing deployment that still relies on relay-selected keys. Persist a visible “relay trusted for compute identity” label and warn before sending. Preserve encrypted API v1 and existing safeguards; no plaintext or legacy-route fallback. |
| Optional TOFU | Explicit weaker choice that pins the first observed operator/root fingerprint. It cannot detect a malicious first relay or first-use substitution. Subsequent unexplained changes block; storage loss/reinstall loses continuity. Independently confirming the fingerprint is required to upgrade to verified status. |

Never convert verified mode to compatibility/TOFU after a timeout or failure.
An explicit user policy change may create a compatibility profile; it must not be
an automatic retry of already queued prompts. Trust errors offer inspect/import,
choose another already trusted operator, or cancel; no generic “ignore” button.
Existing installations need an informed migration choice, not silent relabeling as
verified. Policy and labels must be consistent across DSPACE, Python, desktop and
landing-page clients. Trusted-relay mode is a documented threat-model choice, not
an assertion of protection against its trusted relay becoming malicious.

## Stages, dependencies and review gates

1. **Design gate (this PR):** approve threat model, independent-root preference,
   compatibility labels and ownership with K061/K069 reviewers. Parent coordinator
   links this PR and follow-ups on K061; board tracking does not mean implementation
   is complete. No deployment is authorized by this design.
2. **Protocol gate:** cryptographic review of the selected profile, exact schema,
   canonicalization, nonce/replay state, clock policy, signature placement and
   library support for Python/browser/desktop. Resolve payload limits, revocation
   freshness/skew numbers, issuance custody and emergency recovery procedures.
   Publish shared positive/negative vectors before implementing clients.
3. **Implementation gate:** separate reviewed K069/enrollment/client work; operator
   tooling and trust-store persistence must exist before advertising verified mode.
   Keep legacy encrypted compatibility behind explicit policy. Independently audit
   client delivery/update integrity and operator key handling.
4. **Offline interoperability gate:** execute the tests below across all clients,
   including old/new combinations and rollback. Neither docs CI nor a working
   honest-relay happy path demonstrates malicious-relay resistance.
5. **Rollout gate:** separately authorized staging validation with synthetic data,
   then opt-in existing-client migration and verified defaults for new clients once
   bootstrap is usable. Collect only bounded error/mode counts, never content,
   keys, signatures or raw user identifiers. Security and client owners approve
   production separately. Rollback stops verified dispatch or restores a known-good
   verifier; it cannot silently reduce trust. Retire compatibility only after an
   announced migration window and measured client readiness.

Remaining protocol questions include browser/library support, certificate encoding
and issuance limits, root recovery UX, replay persistence across compute replicas,
retry cost/idempotency, privacy of operator metadata, and whether a separate
forward-secret session protocol is needed. These are release gates, not permission
to improvise wire cryptography while calling it verified.

## Offline regression and negative-test plan

Use local fake relays, deterministic fixtures, generated test keys and synthetic
sentinels. No production credentials, live compromise tests or deployment probes.
Existing invariant tests remain mandatory; proposed cases are not yet implemented.

- Reproduce relay key substitution on both current DSPACE/Python test paths; confirm
  verified clients reject an unsigned key, wrong operator chain, substituted
  encryption/signing key, and a descriptor from an untrusted root before dispatch.
- Forge a response using only the public client key and observed routing fields;
  require signature failure. Change request/body/model/options, response recipient,
  request digest, operator/node, version or outer metadata independently; reject
  without rendering content or invoking tools. Include valid encrypted errors and
  forged progress, reordered progress, and progress after terminal completion.
- Exercise bad AEAD tags, truncated messages, wrong signature context, malformed
  keys, duplicate JSON keys, Unicode/numeric edge cases, unknown suites and size
  limits with shared Python/browser/desktop vectors and established RFC vectors.
- Replay across sessions, clients, nodes, restart, expiry and retry; test bounded
  cache exhaustion failing closed, nonce collision handling and one terminal result.
- Test expiration/skew, revoked in-flight keys, root compromise recovery, legitimate
  overlap rotation, stale/missing revocation, epoch rollback and lost local state.
- Strip discovery capabilities, downgrade to legacy envelopes, or force selection
  failure: verified mode must never dispatch compatibility traffic. Test all old/new
  client/compute/relay combinations; incompatible verified peers fail clearly.
- Verify arbitrary `https://example.com` relay configuration with independent roots,
  multiple operators and allowed-operator failover. Reject unexpected root changes
  without a hostname allowlist. Retain #1916 destination-bound credential tests.
- Check bootstrap cancellation, fingerprint mismatch, explicit compatibility labels,
  TOFU first-use limitation and trust-store reset. Demonstrate the mutable-JS threat
  in an isolated fixture and verify the chosen independent delivery boundary.
- Retain ciphertext-only queue/log/diagnostic/egress sentinel assertions and disabled
  historical plaintext-route tests. No negative test may introduce a plaintext
  production fallback or re-enable deprecated endpoints.
