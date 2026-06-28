# Qwen3 8B Q4_K_M API v1 model migration design

Status: design proposal for the P23 migration sequence. This document is
normative for switching token.place API v1 desktop compute from
`Meta-Llama-3.1-8B-Instruct-Q4_K_M` to `Qwen3-8B-Q4_K_M` while preserving API v1
behavior. It does not change runtime behavior by itself.

## Scope and non-goals

### In scope

- Design the API v1 default desktop compute model migration from Llama 3.1 8B
  Q4_K_M to Qwen3 8B Q4_K_M.
- Preserve the existing `8k-fast` and `64k-full` context tiers.
- Preserve API v1 as non-streaming chat completions.
- Preserve relay-blind E2EE: relay-owned state, logs, diagnostics, and payloads
  remain ciphertext-only plus safe routing metadata.
- Preserve API v1 non-reasoning/non-thinking behavior by forcing Qwen3
  non-thinking mode and preventing `<think>` content from reaching clients.
- Define model-profile/catalog plumbing for P23b, Qwen runtime support for P23c,
  token.place default/UI/docs changes for P23d, and DSPACE follow-up for P23e.

### Non-goals

- No runtime code, API behavior, desktop UI, landing page, model default,
  generated artifact, DSPACE, or API v2 change is made by this design document.
- API v2 remains incomplete and explicitly out of scope. Do not route runtime
  traffic through API v2 as part of this migration.
- Do not add streaming to API v1.
- Do not reintroduce deprecated relay endpoints (`/sink`, `/faucet`, `/source`,
  `/retrieve`, or `/next_server`) as compatibility fallbacks.
- Do not add multi-model-per-node serving in this arc. A compute node advertises
  one active model profile and context tier after warm-load validation.
- Do not implement 128K context in this arc. Future 128K work may use YaRN factor
  `4.0` against Qwen3's native 32,768 tokens, but this migration stops at 64K.

## External model facts and assumptions

Qwen's public model card for `Qwen/Qwen3-8B` describes Qwen3 8B as an Apache-2.0
model with native 32,768-token context, long-context validation up to 131,072
tokens using YaRN/RoPE scaling, and support for both thinking and non-thinking
behavior. The `Qwen/Qwen3-8B-GGUF` repository provides GGUF artifacts for local
llama.cpp-style runtimes, including Q4_K_M quantization. Implementation PRs must
re-check the exact model card, GGUF file list, and llama-cpp-python API available
in the build environment before hardcoding runtime parameters.

## Hard invariants

1. **Relay-blind E2EE stays mandatory.** The relay may see only ciphertext plus
   safe routing metadata such as requested model id, context tier, registration
   readiness, and coarse diagnostics. It must never queue, forward, log,
   diagnose, or expose plaintext prompts, messages, tool arguments, model output,
   or decrypted token counts.
2. **API v1 remains the active runtime target.** API v1 chat completions are
   non-streaming and return only after full model output generation.
3. **API v1 remains non-reasoning/non-thinking.** Qwen3 thinking mode must be
   disabled. No `<think>` block may appear in API v1 assistant content.
4. **Exact context admission is compute-owned.** The compute node decrypts the
   request, renders with the same chat-template path used for generation,
   tokenizes with the active runtime tokenizer, and fails closed on overflow.
5. **Runtime context is selected before model load.** The desktop operator's
   active context profile must reach `ModelManager` before constructing the
   llama.cpp runtime. The runtime is not resized per request.
6. **Qwen3 must not use Llama chat formatting.** Any silent fallback to
   `chat_format='llama-3'` for Qwen3 is a bug and must fail tests.

## Current hardcoded Llama touchpoints

P23b-P23d should audit the repository for scattered Llama constants and migrate
active runtime surfaces to the profile/catalog abstraction. The checklist below
is intentionally broader than the P23a doc-only change so implementation prompts
can work file by file.

| Area | Touchpoint | Migration concern |
| --- | --- | --- |
| API model catalog | `api/v1/models.py` | Public id `llama-3.1-8b-instruct`, aliases, owner/metadata, and `/api/v1/models` response shape. |
| Config schema | `utils/config_schema.py` | Model filename, URL, family URL, context size, and future `model.profile_id`/template/thinking/rope fields. |
| Runtime manager | `utils/llm/model_manager.py` | Default GGUF URL/filename, local path resolution, `chat_format='llama-3'`, context size, artifact metadata, and llama_cpp constructor kwargs. |
| Relay client | `utils/networking/relay_client.py` | Active model capability registration, requested-model filtering, alias resolution, diagnostics, and context tier metadata. |
| Desktop Python bridge | `desktop-tauri/src-tauri/python/model_bridge.py` | Fallback download/inspect metadata, environment overrides, default artifact URL/filename/family URL, and safe diagnostics. |
| Desktop Tauri UI/Rust | `desktop-tauri/src/**`, `desktop-tauri/src-tauri/src/**` | Model labels, download state, inspect/load copy, Start Operator metadata, packaging assumptions, and error text. |
| Landing chat JS | `static/chat.js` | Emergency fallback model id, model dropdown defaulting, label/description rendering, and model request payload. |
| Landing HTML | `static/index.html` | Visible copy, fallback labels, metadata hints, and any static Llama references. |
| Release/promotion docs | `docs/releases/`, `docs/PRODUCTION_PROMOTION.md`, `docs/TESTING.md` | Current-model statements, smoke checks, rollback instructions, and historical sections that should not be rewritten. |
| Smoke scripts | `scripts/` | Model catalog assertions, promotion smoke expectations, and future Qwen migration smoke. |
| Tests | `tests/` | API catalog tests, alias tests, relay scheduling tests, ModelManager tests, desktop bridge tests, UI tests, promotion-doc tests, and context profile tests. |
| Downstream DSPACE | `democratizedspace/dspace` follow-up | Runtime default token.place model id, env override docs, smoke prompts, and rollback behavior. |

Audit commands for implementation PRs should use `rg`, for example:

```sh
rg -n "llama-3\.1|Meta-Llama|Llama 3|Q4_K_M|chat_format|DEFAULT_MODEL|model.*url|gpt-3\.5-turbo|gpt-5-chat-latest" \
  api utils desktop-tauri static docs scripts tests
```

## Target model profile

The target Qwen profile is the canonical API v1 model after P23d.

| Field | Value |
| --- | --- |
| Profile id | `qwen3-8b-instruct` |
| API id | `qwen3-8b-instruct` |
| Display name | `Qwen3 8B Instruct` |
| Description | Local Qwen3 8B Q4_K_M non-thinking chat model for token.place API v1 desktop compute. |
| Owner/provider | `Qwen` / `token.place desktop` |
| Source model | `Qwen/Qwen3-8B` |
| GGUF repo | `Qwen/Qwen3-8B-GGUF` |
| GGUF filename | `Qwen3-8B-Q4_K_M.gguf` |
| Quantization | `Q4_K_M` |
| License | `Apache-2.0` |
| Parameters | approximately 8.2B |
| Native context tokens | `32768` |
| Maximum validated context tokens | `131072` upstream validation; token.place initially validates `65536` for API v1. |
| Supported token.place tiers | `8k-fast`, `64k-full` |
| Default mode | non-thinking / reasoning disabled |
| Chat-template policy | use verified Qwen3 GGUF/Jinja or verified runtime Qwen3 handler; never Llama 3 formatting. |
| Context-extension policy | no YaRN for 8K; YaRN/RoPE factor `2.0` over original/native `32768` for 64K. |
| Compatibility aliases | During P23d rollout, map legacy `llama-3.1-8b-instruct` requests to the active Qwen profile only if tests prove scheduling resolves to Qwen-capable nodes. |

The existing Llama profile remains available internally so rollback can switch
back by configuration without deleting either GGUF file.

## Model-profile architecture

token.place should stop scattering model constants across API, desktop, runtime,
and docs surfaces. P23b should introduce a central model profile/catalog
abstraction used by API v1 metadata, ModelManager artifact metadata, desktop
bridge fallback metadata, relay capability registration, and tests.

A profile should include at least:

- canonical API model id;
- aliases and alias intent (`public`, `compatibility`, `invisible`, or
  `rollback-only`);
- display name, short description, and docs labels;
- owner/provider/source model;
- source model URL and canonical family URL;
- GGUF repo id and filename;
- download URL or Hugging Face repo-plus-filename tuple;
- expected quantization;
- license;
- parameter class;
- native context tokens;
- maximum validated context tokens;
- token.place supported context tiers;
- default context tokens;
- context-extension policy, including whether YaRN/RoPE is required above native
  context;
- chat-template policy;
- thinking/non-thinking policy;
- profile-driven generation defaults;
- safe diagnostics labels.

Example schema shape for implementation discussion:

```json
{
  "profile_id": "qwen3-8b-instruct",
  "api_model_id": "qwen3-8b-instruct",
  "aliases": ["llama-3.1-8b-instruct"],
  "display_name": "Qwen3 8B Instruct",
  "owner": "Qwen",
  "provider": "token.place desktop",
  "source_model": "Qwen/Qwen3-8B",
  "gguf_repo": "Qwen/Qwen3-8B-GGUF",
  "filename": "Qwen3-8B-Q4_K_M.gguf",
  "quantization": "Q4_K_M",
  "license": "Apache-2.0",
  "native_context_tokens": 32768,
  "maximum_validated_context_tokens": 131072,
  "supported_context_tiers": ["8k-fast", "64k-full"],
  "chat_template_policy": "qwen3_gguf_jinja_verified",
  "thinking_mode": "disabled",
  "rope_scaling_policy": {
    "type": "yarn_when_context_exceeds_native",
    "native_context_tokens": 32768,
    "tiers": {
      "64k-full": {"factor": 2.0, "original_context_tokens": 32768}
    }
  }
}
```

P23b should keep the default profile as Llama and add Qwen as non-default
metadata. P23c should make Qwen runnable. P23d should switch the default.

## API compatibility

Recommended transition behavior:

1. P23b keeps `llama-3.1-8b-instruct` as the canonical default and does not map
   it to Qwen.
2. P23c keeps the default as Llama but allows explicit Qwen runtime tests.
3. P23d makes `qwen3-8b-instruct` canonical and accepts
   `llama-3.1-8b-instruct` as a tested compatibility alias for at least one
   transition period.
4. P23e updates DSPACE to request `qwen3-8b-instruct` directly after token.place
   staging proves healthy.

`/api/v1/models` should eventually list only the canonical active model unless
maintainers deliberately choose to expose both. During P23b, conservative
behavior is preferred: keep Qwen internal and do not advertise it as selectable
until P23c has correct runtime support. During P23d, the public catalog should
list Qwen as the primary/default API v1 model.

Alias support must be intentional and tested. If a request uses the old Llama id
after P23d, request validation should resolve it to the canonical active Qwen
profile before relay scheduling so only Qwen-capable nodes satisfy the request.
Do not route Qwen requests to stale Llama runtimes.

Existing invisible compatibility aliases such as OpenAI-like ids may continue to
map to the active API v1 model if that is current behavior, but tests must lock
whether those aliases are public, invisible, or deprecated.

## Chat-template handling

Qwen3 must not run through `chat_format='llama-3'`.

Preferred behavior:

1. Use the GGUF/Jinja chat template embedded in `Qwen3-8B-Q4_K_M.gguf` when
   llama-cpp-python exposes it correctly.
2. If llama-cpp-python exposes a Qwen/Qwen3 chat format or chat handler, use it
   only after implementation inspection verifies it matches the Qwen3 model
   card and GGUF template.
3. If no correct template path is available, fail warm-load/startup with a clear
   operator error instead of serving with a guessed or Llama fallback template.

Context admission and generation must use the same render/tokenize path. The
compute node should render the API v1 messages with the active profile's template
policy, tokenize that rendered form with the active runtime tokenizer, and pass
that same template path into generation. Tests must fail if Qwen admission or
generation silently falls back to Llama formatting.

## Non-thinking mode

API v1 is non-reasoning/non-thinking. Qwen3 must be run with thinking disabled.
No `<think>` block may appear in API v1 assistant content.

Implementation PRs must inspect the current llama-cpp-python and Qwen3 template
surface before choosing the exact mechanism. Acceptable mechanisms, in preferred
order, are:

1. pass `enable_thinking=False` or the runtime-specific equivalent into the
   verified Qwen3 Jinja/template call;
2. use a verified llama-cpp-python Qwen3 chat handler that disables thinking;
3. inject a minimal `/no_think` control in the correct model-specific position
   only if the runtime cannot pass a structured flag and tests prove the control
   is not exposed to clients or downstream apps;
4. combine structured flags and `/no_think` only if inspection shows both are
   required for the runtime version in use.

Primary control must be runtime/template configuration. Response validation or a
sanitizer is only a defensive fallback. Preferred fail-safe behavior for a Qwen
API v1 response containing `<think>` is to reject the compute output with a
structured error such as `compute_node_invalid_model_output`, encrypt that error
back through the API v1 response path, and log only safe metadata. If product
requirements later choose stripping instead of rejection, stripping must remove
all reasoning content before client delivery and tests must prove no reasoning
leaks.

Add a startup/model smoke test that asks a simple prompt such as `hi` and asserts
normal assistant text with no `<think>` tags.

## YaRN/RoPE for 64K

Definitions:

- **Model native context** is the context length the source model supports
  without extension. For Qwen3 8B, this is 32,768 tokens.
- **token.place context tier** is an operator/runtime configuration such as
  `8k-fast` (8,192 tokens) or `64k-full` (65,536 tokens).
- **Exact context admission** is the per-request compute-side check that renders
  and tokenizes decrypted messages, reserves output tokens, and rejects requests
  that exceed the active runtime window.

Required behavior:

| Active profile | Tier | Runtime context | YaRN/RoPE behavior |
| --- | ---: | ---: | --- |
| Llama rollback/default before P23d | `8k-fast` | 8,192 | unchanged from current behavior. |
| Llama rollback/default before P23d | `64k-full` | 65,536 | unchanged from current behavior; do not add Qwen-specific YaRN. |
| Qwen3 | `8k-fast` | 8,192 | no YaRN/RoPE scaling because the tier is below native 32,768. |
| Qwen3 | `64k-full` | 65,536 | apply YaRN/RoPE with factor `2.0` and original/native context `32768`. |

Do not apply static YaRN to 8K because Qwen documentation warns long-context
scaling is for conversations that significantly exceed native context, and
short-context behavior may degrade when scaling is always on. Do not implement
128K here.

`ModelManager` must receive the active context profile before loading the model.
The active context profile sets `n_ctx`/runtime context. The model profile then
decides whether context-extension kwargs are needed by comparing active context
tokens to `native_context_tokens`.

P23c must inspect the installed llama-cpp-python API before implementing exact
constructor fields. Candidate fields may include RoPE scaling type, frequency
base/scale, YaRN original context, and YaRN extrapolation/attention factors, but
this document deliberately does not make unverified parameter names normative.
The implementation must fail warm-load/startup clearly if Qwen3 64K requires
YaRN and the installed runtime cannot accept or apply the required settings.

Safe runtime diagnostics should include only metadata such as:

- active model id/profile id;
- GGUF filename and quantization;
- context tier and runtime context size;
- native context tokens;
- chat-template mode;
- thinking disabled/enforced flag;
- rope/yarn enabled boolean;
- rope/yarn factor;
- YaRN original context;
- backend/offload class already allowed by existing diagnostics.

Diagnostics must not include prompts, responses, decrypted token counts, secrets,
full public keys, device serial numbers, or user identifiers.

## Desktop app migration

P23d should update desktop download, inspect, load, and visible labels to Qwen.

Required behavior:

- The default desktop artifact becomes `Qwen3-8B-Q4_K_M.gguf` from
  `Qwen/Qwen3-8B-GGUF`.
- Desktop inspect/download fallback metadata should come from the active profile
  or mirror the same defaults and environment override semantics.
- Existing overrides such as `TOKEN_PLACE_DEFAULT_MODEL_FILENAME`,
  `TOKEN_PLACE_DEFAULT_MODEL_URL`, and `TOKEN_PLACE_DEFAULT_MODEL_FAMILY_URL`
  must continue to work.
- Operators with the old Llama file already downloaded must be prompted to
  download Qwen or shown that the active Qwen artifact is missing. The old Llama
  file must not be treated as satisfying the active Qwen profile.
- Do not delete old Llama files automatically. Keeping both files supports
  rollback and avoids destructive local storage changes.
- UI copy should display `Qwen3 8B Instruct` and Q4_K_M metadata for the active
  profile.
- Download failures should explain the active profile, expected filename,
  repository/URL, and override variables without leaking secrets.
- macOS and Windows packages must include any sidecar/runtime changes needed for
  Qwen3 template and YaRN support. GPU-capable llama-cpp-python builds remain a
  release requirement for desktop GPU modes.
- Desktop operators must restart the operator after switching profile or context
  tier because model context and template settings are fixed at warm-load.

## Landing page migration

The landing page should continue to prefer `/api/v1/models` as the source of
truth. P23d should update static fallback behavior for cases where the model
catalog cannot be fetched.

Required behavior:

- `EMERGENCY_MODEL_FALLBACK_ID` becomes `qwen3-8b-instruct` after P23d.
- The default selected model should be the canonical active API v1 model returned
  by `/api/v1/models`.
- Visible labels and descriptions should say `Qwen3 8B Instruct` or Qwen3 8B
  Q4_K_M, not Llama, except in historical/rollback context.
- Model metadata display should use catalog fields where available.
- Auto / `8k-fast` / `64k-full` context tier behavior remains unchanged.
- Landing API v1 relay chat remains non-streaming and E2EE; it must not call API
  v2 or direct `/api/v1/chat/completions` relay paths if current architecture
  uses relay scheduling.

## Relay and scheduler impact

The relay remains blind to plaintext and model internals. Model migration affects
only safe routing metadata and capability matching.

Required behavior:

- Nodes register the active model capability after warm-load validation, including
  canonical model id/profile id and supported context tier.
- Scheduler filters by resolved requested model and requested context tier.
- Old model aliases must map consistently at validation/scheduling boundaries.
- During mixed rollout, stale Llama nodes and new Qwen nodes may coexist. If
  `llama-3.1-8b-instruct` is a compatibility alias to Qwen after P23d, the relay
  must resolve it to the current Qwen profile and route only to Qwen-capable
  nodes. It must not send Qwen requests to stale Llama nodes merely because the
  client used the old id.
- If maintainers choose hard rejection instead of aliasing, the error must be
  clear and non-catastrophic for DSPACE; however, aliasing is the preferred
  rollout path.
- Diagnostics may expose safe counts and capability labels. They must not expose
  plaintext payloads, decrypted token counts, prompts, responses, or secrets.

## DSPACE impact

DSPACE changes are downstream and belong to P23e after token.place P23d is
staged and healthy.

Required DSPACE behavior:

- Default token.place API v1 chat model id moves from `llama-3.1-8b-instruct` to
  `qwen3-8b-instruct`.
- DSPACE runtime environment override support must continue to work, including
  rollback override to `llama-3.1-8b-instruct` if token.place temporarily rolls
  back.
- If token.place preserves the old Llama id as a compatibility alias, DSPACE can
  migrate after staging without a hard cutover race.
- DSPACE prompt improvements from P16-P22 should remain provider/model-agnostic;
  do not special-case Qwen in prompt planning unless a later quality task proves
  it is necessary.
- DSPACE docs should explain that token.place API v1 remains non-thinking and
  that 8K/64K tier handling, including Qwen 64K YaRN/RoPE, is token.place-side.

## Testing plan

### P23b profile and catalog tests

- API v1 model catalog remains backward compatible while Llama is default.
- Llama default profile id, API model id, filename, URL, aliases, and catalog
  response remain unchanged.
- Qwen profile exists centrally with exact metadata: API id, display name, source
  model, GGUF repo, filename, quantization, license, native context,
  maximum-validated context, supported tiers, thinking disabled, and 64K YaRN
  policy.
- Qwen is not advertised as fully usable before runtime support if the
  conservative catalog behavior is chosen.
- Alias tests prove old aliases still resolve to Llama before P23d.
- `ModelManager.get_model_artifact_metadata()` uses profile values while keeping
  existing backward-compatible keys.
- Desktop bridge fallback reports Llama defaults before P23d.
- Environment override tests still pass for filename, URL, and family URL.

### P23c runtime tests

- With mock/fake `llama_cpp`, Qwen runtime initialization omits
  `chat_format='llama-3'`.
- Qwen runtime initialization uses the verified template mode or fails fast if no
  correct mode is available.
- Context admission uses the same Qwen render/tokenize path as generation and not
  a Llama formatter fallback.
- Non-thinking template kwargs, runtime handler options, or `/no_think` injection
  are applied as inspected and documented.
- Qwen output containing `<think>` is rejected or safely sanitized according to
  the chosen policy, with no reasoning text delivered to clients.
- Qwen `8k-fast` does not enable YaRN/RoPE.
- Qwen `64k-full` enables YaRN/RoPE with factor `2.0` and original context
  `32768`.
- Llama 8K/64K behavior remains unchanged.
- Warm-load fails clearly for Qwen 64K if llama-cpp-python cannot support the
  required YaRN/RoPE settings.
- Runtime diagnostics include safe model/template/thinking/rope metadata and no
  plaintext.

### P23d default, desktop, landing, and relay tests

- `/api/v1/models` lists Qwen as the primary/default API v1 model.
- `llama-3.1-8b-instruct` compatibility alias behavior is explicit and tested.
- Existing invisible aliases continue mapping according to current policy.
- Default ModelManager filename, URL/repo, profile id, and metadata are Qwen.
- Desktop bridge fallback reports Qwen defaults.
- Desktop UI tests expect Qwen labels and download state.
- Operators with only the old Llama file are prompted to download Qwen and are
  not considered ready for the Qwen profile.
- Landing page emergency fallback and visible model labels are Qwen.
- Landing page model dropdown uses `/api/v1/models` where available.
- Relay scheduling resolves Qwen canonical id and old compatibility alias to
  Qwen-capable nodes only.
- Mixed Llama/Qwen node tests prove Qwen requests do not route to stale Llama
  runtimes.
- No API v2 expectations change.

### Integration, staging, and manual tests

- API v1 desktop bridge E2EE integration tests continue to prove ciphertext-only
  relay behavior.
- Exact admission tests cover near-limit prompts for `8k-fast` and `64k-full`.
- Startup smoke prompt `hi` succeeds with Qwen and contains no `<think>`.
- Landing page Auto + `hi` succeeds with no `<think>`.
- Landing page 64K Full with a known large synthetic prompt succeeds when within
  context and fails closed when over context.
- DSPACE smoke prompts after P23e:
  - `hi`
  - `do I have enough green PLA?`
  - `I’d like to make a 3D printed rocket and 10 benchies. Is it enough for that?`
  - `where do I import a gamesave?`
  - a known 64K-ish synthetic/private-safe prompt
- Manual staging records requested tier, selected tier, coarse prompt-token band
  if available to the compute node, latency band, no `<think>`, and response
  sanity. Do not paste plaintext user content into relay logs or shared
  diagnostics.

## Rollout plan

1. **P23a: design only.** Land this document. No runtime behavior changes.
2. **P23b: profile plumbing.** Add central model profiles and Qwen metadata while
   keeping Llama as the default and not advertising Qwen as fully usable unless
   runtime support is safe.
3. **P23c: Qwen runtime support.** Implement verified Qwen chat-template path,
   non-thinking enforcement, response validation, Qwen 64K YaRN/RoPE, and safe
   diagnostics. Keep default Llama.
4. **P23d: token.place default switch.** Switch staging default/profile/catalog,
   desktop app, landing page, docs, and tests to Qwen. Keep old Llama id as a
   tested compatibility alias unless maintainers deliberately choose hard
   rejection.
5. **Staging bake.** Verify `/api/v1/models`, desktop 8K, desktop 64K,
   diagnostics, landing chat, old-id alias behavior, no `<think>`, and DSPACE
   compatibility before production.
6. **P23e: DSPACE default switch.** Update DSPACE to request
   `qwen3-8b-instruct` by default while preserving env override rollback.
7. **P23f: operational smoke/runbook.** Add ad-hoc smoke script and rollback
   runbook after migration behavior exists.

Operational logs and diagnostics should verify active model/profile/template and
context metadata only:

- active API model id and profile id;
- display name;
- filename and quantization;
- context tier and runtime context size;
- chat-template mode;
- thinking disabled/enforced;
- YaRN enabled/factor/original context for 64K;
- backend/offload class.

## Rollback plan

Rollback should be a profile/default switch, not a destructive file operation.

- Keep both Qwen and Llama profiles in the catalog.
- Keep both GGUF files on operator machines if they have been downloaded.
- Do not delete Qwen or Llama files automatically during rollback or roll-forward.
- Switch token.place default profile back to `llama-3.1-8b-instruct` by env/config
  if supported by P23b/P23d plumbing.
- Restart desktop operators so `ModelManager` loads the rollback profile and
  context settings from a clean warm-load.
- Verify `/api/v1/models` and relay diagnostics report the rollback profile.
- Verify landing fallback/default model id matches rollback behavior.
- In DSPACE, use the existing runtime env override to request
  `llama-3.1-8b-instruct` if token.place must temporarily roll back after P23e.
- Verify old-id and Qwen-id behavior according to the alias policy chosen during
  rollback. If Qwen is temporarily unsupported, errors must be clear and
  non-catastrophic.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Wrong chat template | Bad quality, malformed prompts, or hidden runtime failures. | Profile-owned chat-template policy; inspect llama-cpp-python; fail fast if Qwen would use Llama formatting; tests for admission and generation paths. |
| Thinking output leakage | API v1 violates non-reasoning contract and may expose internal reasoning. | Disable thinking at template/runtime level; add `<think>` response validation; startup smoke test; safe encrypted error on leakage. |
| YaRN not applied at 64K | Qwen 64K tier may fail, degrade, or exceed native context incorrectly. | Context-extension policy tied to active profile+tier; warm-load validation; diagnostics show YaRN enabled/factor/original context; tests for 64K kwargs. |
| YaRN degrading 8K | Short-context quality may regress. | Apply YaRN only when active context exceeds native 32,768; explicit 8K no-YaRN tests. |
| GGUF download mismatch | Desktop loads wrong artifact or reports ready incorrectly. | Profile-owned filename/repo/URL; size/existence metadata; old Llama file does not satisfy Qwen profile; clear download errors. |
| Mixed Llama/Qwen nodes during rollout | Requests may route to incompatible nodes. | Resolve aliases before scheduling; nodes advertise canonical active model; tests ensure Qwen requests route only to Qwen nodes. |
| Stale desktop app bundle | Operator UI/runtime may show old Llama defaults or lack Qwen template/YaRN support. | Require desktop rebuild/reinstall for P23d; expose safe diagnostics; staging checklist confirms bundle version and active profile. |
| DSPACE requesting old model id | Downstream requests may fail during cutover. | Keep old id as compatibility alias during transition; P23e updates default after staging; env override supports rollback. |
| Benchmarks do not translate to DSPACE quality | Qwen may differ from Llama on domain-specific prompts. | Manual DSPACE smoke prompts; retain provider/model-agnostic prompt shaping; use staging bake before P23e; keep rollback profile. |
| llama-cpp-python API drift | YaRN/template kwargs may differ by installed version. | Inspect runtime APIs in P23c; encode decisions in comments/tests; fail fast when required support is unavailable. |
| Relay privacy regression | Model diagnostics accidentally include plaintext or sensitive device data. | Reuse relay-blind E2EE invariant; diagnostics allow only safe routing/model metadata; tests and reviews reject plaintext fields. |

## Acceptance criteria for the migration sequence

- P23a design is detailed enough for P23b-P23e implementation prompts.
- P23b centralizes model metadata and adds Qwen profile without changing the
  default model.
- P23c makes Qwen runnable without using Llama formatting, with non-thinking
  enforcement and Qwen 64K YaRN/RoPE factor `2.0` over `32768`.
- P23d switches token.place API v1, desktop, landing page, docs, and tests to
  Qwen while preserving DSPACE compatibility through tested alias behavior.
- P23e updates DSPACE to request `qwen3-8b-instruct` by default while preserving
  runtime override rollback.
- API v1 remains non-streaming, non-thinking, and relay-blind E2EE throughout.
- API v2 remains untouched except for explicit non-use statements.
