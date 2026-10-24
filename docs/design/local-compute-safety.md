# K-376: local compute-node safety screening

Status: staged design only. This PR changes documentation, not runtime behavior,
routing, dependencies, credentials, logging, or deployment. No classifier has been
selected or evaluated. K-376's deliverable is this design PR; the stages below
describe later implementation and evidence, not additional approval before this PR.

## Goal and boundaries

Screen requests on the selected compute node **after E2EE decryption and envelope
validation, before LLM inference**. Clear policy failures must refuse generation.
Request plaintext, classifier inputs, scores, explanations, and refusal details
must not go to relay-owned state, logs, diagnostics, or a hosted moderation service.
Screening must remain local even during outages. Safety is not blanket permission
to weaken privacy or bypass the [API v1 E2EE contract](../architecture/api_v1_e2ee_relay.md).

The first useful slice is a request gate, not an assurance that generated output
is safe. Output screening, model alignment, and operational abuse controls remain
separate layers. The compute operator already receives plaintext and can retain it
or disable screening; this design cannot make an untrusted operator honest.
The [K061/K069 trust design](verified-compute-trust.md) remains proposed protection,
not an implemented guarantee of independently authenticated compute or responses.

## Verified baseline

Source inspected at `c3e0f90f00745044adeeb1871ea3da96c82baf11` on 2026-10-24:

| Source | Current behavior and design consequence |
| --- | --- |
| [Distributed and local providers](../../api/v1/compute_provider.py) | `DistributedApiV1ComputeProvider.complete_chat` creates request-specific crypto and calls `/api/v1/relay/servers/next` per request, requesting model eligibility and `8k-fast`. `LocalApiV1ComputeProvider` invokes the local generator in process. Do not describe both as distributed selection. |
| [Scheduler](../../relay_state_store.py) | Filters model/context eligibility, health, draining, concurrency and queue capacity; prefers the smallest capable active tier, then least load, then round-robin ties in registration order. Load includes reservations, queued work and claimed work. `_node_queues` holds assignments for a selected node, not a shared queue that any node may consume. |
| [Landing-page client](../../static/chat.js) | `sendMessageApiOnce` calls `ensureSelectedServer` with `forceReselect: true` for each dispatch. Retained history is encrypted to the selected node; another turn or failover may expose that context to a different operator. There is no conversation affinity guarantee. |
| [Compute request path](../../utils/networking/relay_client.py) and [shared runtime](../../utils/compute_node_runtime.py) | The relay client decrypts, extracts the API v1 envelope and performs runtime generation. Place the future gate after request/schema/key binding validation and before `_generate_api_v1_response_with_runtime_model` can execute completion. Cover server and desktop consumers of the shared path. |
| [Existing moderation](../../api/v1/moderation.py) and [API routes](../../api/v1/routes.py) | Optional, disabled-by-default substring matching returns allowed/blocked and can carry matched text. It is not a calibrated three-way classifier or proof of coverage of the distributed compute path. Do not copy its plaintext reason/logging behavior into relay responses. |
| [Relay store construction](../../relay.py) | `_new_api_v1_relay_state_store` defaults to memory and has an explicit Valkey option. Current single-replica memory-backed operation remains the operational constraint until migration is verified. The Valkey option exists in source; that is not evidence of live HA or globally consistent abuse state. |

API v1 remains text-only and non-streaming. No v2, streaming, legacy-route fallback,
or routing changes are part of this design PR. Historical roadmap checkboxes do
not supersede current architecture guardrails.

## Decision contract and integration

Use one versioned local policy adapter with bounded input size, memory, execution
time and concurrency. Freeze the effective messages/options for a request before
screening, and ensure inference consumes those same validated messages. Include
retained context in the assessment without counting previous assistant output or
quoted content as a fresh user violation. Oversized/truncated or unsupported
language inputs must not silently receive an allow decision.

| Decision | Proposed behavior | Abuse evidence |
| --- | --- | --- |
| `allow` | Continue to existing context admission and non-streaming inference. This is permission under this policy version, not a safety guarantee. | None. |
| `refuse` | For a clear policy failure, do not call LLM inference. Return a fixed, locally authored explanation in the encrypted response with stable code `safety_policy_refused`. | A refusal alone is insufficient for a strike or ban. |
| `uncertain` | Abstain. A bounded second local check may resolve ambiguity; otherwise return encrypted `safety_review_required` without inference in the initial design. Invite clarification without accusing the user. | None. Never convert uncertainty into abuse. |
| Screening unavailable | Timeout, missing/corrupt classifier, incompatible policy, resource exhaustion or invalid classifier output yields encrypted `safety_screening_unavailable`; no inference in enforcement mode. | None. Do not accrue strikes or report outages as violations. |

These are proposed codes, not existing API fields. Keep code meaning stable across
classifier revisions; version the policy, model artifact digest, preprocessing,
thresholds and decision schema independently. Return only a bounded public reason
and policy version to the client inside the encrypted envelope. Do not include
matched text, internal scores, hidden rules or classifier explanations. Encrypted
refusals must obey the existing terminal lifecycle, cancellation and deadline
rules so they release capacity exactly once. Never turn a refusal into an automatic
retry across other nodes in search of a permissive policy.

All `safety_*` outcomes above are errors inside encrypted `api_v1_response.error`,
not successful assistant completions or plaintext relay HTTP errors. A supportive
fixed explanation belongs to that encrypted error's message. New clients must
render a clear error/recovery state rather than an empty successful answer. Test
old clients against unknown error codes: they must retain a generic error and
must not automatically retry it or treat it as a successful completion. Do not
roll out enforcement to a client/version combination that fails that contract.
For the initial gate, `safety_screening_unavailable` is terminal for that request:
neither relay nor client automatically retries it elsewhere. The user may choose
to try again later. Existing client-owned transport failover remains bounded and
must preserve E2EE and eligibility; a safety error cannot enter that path.

Enforcement here is a cooperating-node property, not a network-wide guarantee.
Users can submit a new request, and operators can disable or weaken their own
checks. Any future advertised screening/version capability is only an operator
claim, not proof of execution. Requiring such a capability would need separate
client/assignment eligibility and downgrade design; this PR changes no routing.

`safety_review_required` means local abstention and a request for clarification;
it does not promise human review. A gate deadline expiring, including during the
second check, produces `safety_screening_unavailable`. Operator pause uses that
same terminal code for pending unscreened work. Implementation owners must run
compatibility fixtures for the landing page and supported first-party API clients
and publish the tested versions. There is no reliable client-version signal today;
third-party clients remain unverified, and no network-wide compatibility claim is
permitted. For direct local-provider API calls, use the same error codes in the
ordinary API error shape over that direct connection; there is no distributed
E2EE envelope to unwrap. This does not authorize plaintext errors or content on
relay-owned distributed paths.

Treat every supplied message, including apparent system instructions, quoted
documents and classifier-directed prompts, as untrusted input to the policy
engine. The policy specification and thresholds live outside that input. A user
cannot select a weaker version or override the decision through role spoofing.
Structured output reduces parsing failures; it does not establish correctness.

## Candidate assessment and offline evaluation

Compare fast local text classifiers: a small discriminative encoder, a compact
local guard model, and a conservative rule baseline. Rules are cheap but brittle
to wording and context; encoders may be fast but need calibrated labels and
language coverage; guard models may handle nuance with higher latency and memory
cost. Benchmark on supported CPU and GPU nodes alongside normal compute capacity.
Do not introduce a dependency until artifact provenance, redistribution and usage
licenses, local execution, telemetry behavior and resource costs are verified.

Jev is an option to investigate, **not a selected dependency**. TypeSafe's
[announcement](https://typesafe.ai/blog/introducing-system-one-models-and-jev) and
[documentation](https://docs.typesafe.ai/) describe typed probabilistic decisions
and hosted API usage. Their claims are not token.place benchmarks or proof of
safety accuracy. Local weights, local deployment feasibility and licensing have
not been verified. Hosted moderation of user plaintext is incompatible with this
design. Do not call Jev with user data or use its outputs for distillation without
verifying the relevant license and terms. This PR performs no model calls.

Before implementation rollout, publish a reproducible evaluation using only
synthetic or appropriately licensed public examples. Separate development,
calibration and held-out test sets by source/scenario to reduce leakage. Freeze
labels, splits, thresholds and artifact versions before scoring the held-out set.
Use independent human adjudication for ambiguous labels, record disagreement,
and avoid treating another model's answer as unquestionable ground truth. The
implementation task must identify a maintainer accountable for policy categories
and qualified domain/language reviewers; document adjudicator selection and label
dispute resolution. Predeclare an agreement metric (for example Cohen's kappa for
two raters), its minimum threshold and category sample sizes. Do not label a slice
validated when disagreement exceeds the declared threshold.

Report at least:

- Per-category false positives and false negatives, denominators and confidence
  intervals; precision/recall, confusion matrix and abstention/coverage rate.
- False refusals of benign technical, educational, historical, fictional and
  quoted content, plus mental-health help-seeking and distress. Measure any
  proposed abuse-strike precision separately from generation-refusal accuracy.
- Language and dialect slices, context lengths and mixed benign/harmful turns;
  Unicode/encoding tricks, role spoofing, prompt injection, paraphrases, indirect
  requests, repeated probing, and long-context dilution. Unsupported inputs abstain.
- Calibration reliability and threshold sensitivity, including distribution shift.
  A confidence number is not transferable across versions without validation.
- Warm and cold p50/p95/p99 latency, peak RAM/VRAM, throughput and concurrency
  interference on CPU/GPU; timeouts, crash behavior and cancellation. Measure the
  whole gate, not just classifier execution on a short input.

Numerical latency, false-refusal, dangerous false-negative and coverage budgets
remain undecided. Record them by risk category and supported hardware before an
enforcement rollout; do not choose them retrospectively to pass the test set.
Give the optional second check one attempt inside the same total gate deadline
and resource budget, not a fresh unbounded budget. Publish abstention rates and
ceilings per supported language. Exceeding a ceiling blocks rollout for that
language until corrected; disclose unsupported-language limitations and provide
fixed recovery guidance, never silently relabel uncertainty as allow or abuse.
No measured performance or local Jev feasibility is claimed here.

## Refusal is not a ban

Distress, self-disclosure, mental-health help-seeking, or a request for support must
not automatically earn abuse strikes. A request for harmful instructions can be
refused while still returning a fixed supportive response; that refusal does not
establish malicious intent. Ordinary sensitive topics and uncertain classification
must not be treated as repeated abuse. Ban evidence needs substantially higher
precision and independent context than a harmful-generation refusal.

Repeated **qualifying** violations may eventually cause a node-local cooldown of
X minutes, but the qualification rubric, count N, observation window W, X, decay,
maximum duration, and recovery process are undecided policy parameters. Keep this
stage disabled until its identity and privacy questions are resolved. Count only
distinct, validated requests meeting the separate abuse rubric; transport retries,
replays and the same retained history must never multiply strikes. Expire entries
and decay counts on a defined clock; cap extensions so repeated reports cannot
create an accidental permanent ban. Outages, restarts and missing state must not
reconstruct accusations or increase punishment. A temporary cooldown must expire
automatically and offer a comprehensible recovery path.

There is no reliable node-local person identity today. The distributed Python
provider creates per-request crypto; browser key continuity is neither a person
nor strong Sybil resistance. A compute node should not learn the user's IP just
to implement cooldowns. Any future session pseudonym or hashed identifier, even
in transient memory, introduces linkability and needs explicit analysis of scope,
rotation, retention, collision, evasion and cross-node correlation. Do not add
user logging or reputation storage in the initial request gate. This limits abuse
enforcement; acknowledge that limit instead of silently adding tracking.

## Future relay reports: allegations only

A future API v1 report route is deferred. A compute node must never submit an
arbitrary IP target or unilaterally select an IP ban. Authentication proves who
sent a report, not that the alleged violation occurred. Keep relay authorization
and adjudication separate from the reporting node, and do not automatically turn
report volume or a classifier score into a network-wide ban.

If pursued, bind every authenticated report to an actual, unexpired assignment:
reporter registration/control identity, selected node, opaque request/assignment
identifier, policy/artifact version, bounded allegation category, nonce and
expiry. Verify ownership against authoritative relay state, reject unknown or
stale assignments, deduplicate exactly once, and bound report size, rate and
storage per node and globally. Use trusted relay-observed routing metadata only
if attribution is justified; never accept node-asserted IPs or client-controlled
forwarding headers as authority. These bindings require coordinated protocol
work; current authentication must not be overstated as semantic proof.

| Threat | Required limitation and remaining risk |
| --- | --- |
| Malicious compute node invents failures | Assignment binding limits victims to actual assignments; independent adjudication and reporter suspension/recovery are needed. A legitimate assignment still permits a false allegation. |
| Sybils or colluding nodes fabricate consensus | Admission controls and per-operator/global limits can raise cost, but several identities do not establish independent evidence. No simple quorum authorizes a ban. |
| Replay and stale reports | Unique assignment-bound deduplication, bounded freshness and expiration; cancellation/terminal races must not revive state or multiply punishment. |
| Arbitrary IP targets | Reject caller-selected targets; do not give nodes a general IP-block interface or disclose client IPs to them. |
| Shared NAT/VPN or dynamic addresses | IPs identify network paths, not people. Shared-user collateral and reassignment make automatic IP bans unsuitable as the default remedy. Prefer narrow, reversible controls. |
| Evasion through keys, nodes, relays or addresses | State limits honestly; avoid escalating to pervasive identity tracking to promise complete prevention. |

Minimal evidence means assignment provenance and a bounded allegation, not prompt
excerpts, response text, embeddings, content hashes or classifier reasoning.
Even categories, timing and identifiers can expose sensitive interests and enable
correlation. Evaluate that privacy cost before collecting them. Without plaintext,
the relay may be unable to adjudicate semantics; that is a real limit, not grounds
for compulsory disclosure. Offer an appeal/recovery path using an opaque receipt,
bounded retention, automatic expiry and reversal of mistaken restrictions, without
requiring prompt disclosure. Any optional evidence submission needs its own clear
consent and deletion rules. Resource-abuse controls can remain distinct from
content allegations.

Zero-knowledge proofs are deferred complexity. Proving that a particular classifier
executed on a committed input would not prove that its semantic judgment is correct,
that the policy is fair, or that the input belongs to the accused person. Input,
assignment and identity binding plus replay protection would still be required;
proofs must not be presented as a shortcut to trustworthy bans.

## Stages and release evidence

1. **Offline contract and evaluation:** define policy categories, the three-way
   adapter and version manifest; compare licensed local candidates on the frozen
   synthetic/public corpus. Publish results and failure slices without user data.
2. **Local request gate:** integrate the shared compute path and relevant local
   provider path without relay plaintext. Verify allow invokes inference once;
   refuse, unresolved uncertainty and outage invoke it zero times. Test encrypted
   errors, cancellation, expiry, capacity release, identical screened/generated
   context and no content in logs. Cover server and desktop, mixed versions and
   invalid/missing artifacts. The desktop bridge's GPU qualification path wraps
   `_generate_api_v1_response_with_runtime_model`; place the gate at the shared
   validated request boundary above generation wrappers/recovery and prove exactly
   one gate decision per request on both paths. Do not infer bypass solely from
   that qualification wrapper. Count screening against the same claimed-work
   capacity until terminal release; do not add unaccounted classifier workers.
   Test bounded classifier queues, full queues, cancellation during screening,
   recovery and timeout without double release or duplicate strikes. Deploy only
   through later implementation work.
3. **Measured enforcement:** pin policy/model versions, use staged rollout and a
   known-good local rollback artifact. Reject unsupported versions explicitly;
   never silently downgrade enforcement or use hosted fallback. Aggregate only
   bounded operational health metrics after privacy review, without user labels,
   message-derived values or request-level histories. Do not retain live user
   prompts for shadow evaluation. Stop rollout if predefined budgets fail.
   Provide an explicit operator-visible paused/draining state for a bad screening
   artifact: stop admitting work and fail pending unscreened work safely, then
   restore a verified local artifact. Pausing must not serve unscreened requests
   while claiming enforcement. Test pause/resume and rollback under load.
4. **Optional local cooldown:** first settle the stronger abuse rubric, privacy
   and identity scope, expiry/decay, fairness and recovery tests. Initial gate
   success alone is not enough to enable reputation or cooldown state.
5. **Optional relay allegations:** separately validate authenticated assignment
   binding, Sybil/collusion resistance, replay races, collateral damage, appeal,
   retention and deletion. Remain disabled if a defensible adjudication process
   cannot operate within the privacy boundary.

Future sticky routing is only an option. It would require current model/context
eligibility, health/load admission, bounded affinity expiry and failover; the client
must re-encrypt to each newly selected node. Reusing ciphertext across nodes is not
valid. Affinity increases operator linkability and may reduce node-local cooldown
evasion, but failover and key rotation still permit evasion. It must not trap a
user on an unavailable or malicious node, nor become an implicit tracking system.
Nothing here changes the current per-request scheduler or assignment queues.

## Safety, privacy and security tradeoffs

False negatives can enable severe harm, including scalable abuse and assistance
with catastrophic biological, chemical, cyber or other dangerous capabilities.
Rare catastrophic failures cannot be dismissed by good average benchmark scores;
use specialized synthetic/public evaluations, expert review and layered defenses.
A small classifier cannot establish the absence of such risk. Do not publish
operational harmful examples or collect user content to fill evaluation gaps.

False positives deny legitimate help and disproportionately burden some languages
or vulnerable users. Fail-closed uncertainty protects against unscreened generation
but creates availability and denial-of-service costs. Bound classifier work and
queues; distinguish outages from abuse and provide fixed helpful recovery messages.
Local screening adds CPU/GPU pressure and a supply-chain attack surface: review
artifact provenance, pin versions, avoid executable model loaders where possible,
and constrain permissions and network access in later implementation.

E2EE does not hide response timing or ciphertext length. A quick, small refusal
can reveal likely screening outcomes to the relay or a network observer. Treat
this as residual leakage in the first gate; do not claim content-independent
metadata. Evaluate bounded padding and timing buckets separately against bandwidth,
latency and denial-of-service costs before adoption. They reduce some distinctions
but do not eliminate traffic analysis or justify sending plaintext explanations.

No user logging is introduced now. Any future user logging must be explicitly
opt-in with a specific purpose, minimum data fields, short defined retention,
access controls, deletion across replicas/backups and a way to withdraw consent.
Do not condition ordinary service on logging consent. Hashed identifiers, transient
reputation and aggregate metrics are not automatically anonymous; analyze small
cohorts, timing and linkage. Safety measures must preserve the relay-blind boundary
and be assessed for both security value and privacy cost.
