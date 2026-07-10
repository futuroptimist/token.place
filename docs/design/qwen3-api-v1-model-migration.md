# Qwen3 8B Q4_K_M API v1 model migration design

## Status and scope

This document designs the controlled migration of token.place API v1 from
`Meta-Llama-3.1-8B-Instruct-Q4_K_M` to `Qwen3-8B-Q4_K_M` as the default desktop
compute model. It is intentionally design-only: it does not change runtime code,
API behavior, desktop UI, landing page behavior, model defaults, generated
artifacts, API v2, or DSPACE.

The migration must preserve these API v1 invariants:

- API v1 remains the active runtime target and remains non-streaming.
- Chat completions remain non-reasoning/non-thinking from the client contract's
  perspective.
- Relay-owned state remains relay-blind E2EE: ciphertext plus safe routing
  metadata only. Relay logs, diagnostics, queues, and schedulers must not expose
  plaintext prompt, message, response, tool-argument, or model-output content.
- The existing context tiers remain `8k-fast` and `64k-full`.
- API v2 remains untouched except to explicitly leave it outside this migration.
- DSPACE stays compatible during rollout and later moves to the new canonical
  model id only after token.place staging is healthy.

External model-card facts used by this design should be rechecked in the
implementation PRs before hardcoding runtime behavior. The Qwen model card for
`Qwen/Qwen3-8B-GGUF` currently describes Qwen3-8B as an 8.2B-parameter model,
Apache-2.0 licensed, with native context length 32,768, YaRN support up to
131,072 tokens, and Q4_K_M GGUF artifacts. It also documents thinking and
non-thinking modes. [Qwen/Qwen3-8B-GGUF model card](https://huggingface.co/Qwen/Qwen3-8B-GGUF)

## Migration sequence

This design is the P23a artifact. The implementation sequence should remain
small and reversible:

1. **P23b: model-profile plumbing.** Add a centralized model profile/catalog and
   Qwen metadata while keeping Llama as the default.
2. **P23c: Qwen runtime support.** Make the Qwen profile runnable by adding the
   correct chat-template path, non-thinking enforcement, and 64K YaRN/RoPE
   configuration. Llama remains default.
3. **P23d: token.place default switch.** Switch API v1, desktop, landing page,
   docs, and tests to Qwen as the default while keeping compatibility aliases
   intentional and tested.
4. **P23e: DSPACE follow-up.** Update DSPACE to request `qwen3-8b-instruct`
   after token.place staging proves healthy.
5. **P23f: operational proof.** Add ad-hoc smoke scripts and rollback runbooks.

## Goals

- Replace the default local API v1 model family from Llama 3.1 8B Instruct Q4_K_M
  to Qwen3 8B Q4_K_M.
- Keep the parameter class roughly 8B and quantization at Q4_K_M.
- Preserve exact API v1 request/response behavior wherever practical.
- Preserve `8k-fast` and `64k-full` context tiers.
- Use Qwen's non-thinking mode so API v1 assistant content never includes
  `<think>` blocks.
- Apply YaRN/RoPE scaling only for Qwen contexts above the 32,768-token native
  context length, initially `64k-full` with factor `2.0` over original context
  `32768`.
- Avoid static YaRN in `8k-fast` because it is below native context and may
  degrade short-context behavior.
- Keep DSPACE migration decoupled so token.place staging can be validated first.

## Non-goals

- No API v2 runtime migration.
- No API v1 streaming.
- No multi-model-per-node serving architecture.
- No legacy relay endpoint revival (`/sink`, `/faucet`, `/source`, `/retrieve`,
  `/next_server`).
- No DSPACE code changes in token.place PRs.
- No 128K context tier in this arc. Future 128K work may use YaRN factor `4.0`
  against native 32,768, but that belongs to a separate design and test matrix.

## Current hardcoded Llama touchpoints and repo-audit checklist

The migration should begin with an audit that distinguishes active runtime
surfaces from historical docs/tests. P23b-P23d should check at least these paths.
A focused search should include the root relay entrypoint, for example:

```sh
rg -n "llama|DEFAULT_MODEL_IDS|supported_model_ids" \
  api/v1 utils relay.py desktop-tauri static docs tests scripts
```


- [ ] `api/v1/models.py`
  - Public `/api/v1/models` ids and display metadata.
  - Alias resolution for OpenAI-compatible ids and any invisible compatibility
    aliases.
  - Validation paths that decide whether a requested model is supported.
- [ ] `utils/config_schema.py`
  - Default model filename, URL, family URL, context, and runtime knobs.
  - Any schema fields that assume one Llama artifact.
- [ ] `utils/llm/model_manager.py`
  - Default GGUF filename and URL.
  - Model path resolution and download/inspect metadata.
  - llama.cpp construction defaults, especially `chat_format='llama-3'`.
  - Context-size handling and exact context admission tokenization.
  - Startup diagnostics and artifact metadata.
- [ ] `utils/networking/relay_client.py`
  - Node capability registration, `supported_model_ids`, active context tier,
    artifact metadata, and safe diagnostics.
  - Compute-side model mismatch handling after decrypting the request.
- [ ] `relay.py`
  - Relay default capability plumbing: `DEFAULT_MODEL_IDS`,
    `_api_v1_default_capabilities()`, and `supported_model_ids` in safe
    selected-server metadata.
  - Scheduler rejection/filtering behavior for unsupported requested models,
    including nodes omitted because `requested_model` is absent from
    `supported_model_ids`.
- [ ] `desktop-tauri/src-tauri/python/model_bridge.py`
  - Desktop fallback metadata when the Python model manager cannot be imported.
  - Environment overrides such as `TOKEN_PLACE_DEFAULT_MODEL_FILENAME`,
    `TOKEN_PLACE_DEFAULT_MODEL_URL`, and `TOKEN_PLACE_DEFAULT_MODEL_FAMILY_URL`.
- [ ] Desktop Tauri React/Rust files that show model names or download state
  - Inspect/download/load labels.
  - Errors that mention Llama directly.
  - Any Rust command payloads or TypeScript types that assume one model family.
- [ ] `static/chat.js`
  - Landing chat emergency fallback model id.
  - Default selected model behavior.
  - Any hardcoded labels or model metadata fallback.
- [ ] `static/index.html`
  - Visible model labels, descriptions, placeholders, and help text.
- [ ] Release docs, smoke scripts, promotion docs, design/architecture docs, and tests
  - Current operational docs such as production promotion checks.
  - Companion design docs with schema examples, including
    `docs/design/context-tiered-compute.md` context profile `model_ids` and
    capability registration `supported_model_ids` examples that currently name
    only `llama-3.1-8b-instruct`.
  - Smoke scripts that require exactly `llama-3.1-8b-instruct`.
  - Unit/integration tests that encode Llama as the only active default.
  - Historical release notes should remain historically accurate; only present
    behavior docs should change in P23d.
- [ ] Downstream DSPACE runtime model default
  - Treat as P23e follow-up in `democratizedspace/dspace`, not token.place.
  - Preserve DSPACE runtime/env override behavior during the token.place rollout.

## Target Qwen model profile

The intended Qwen profile is:

| Field | Value |
| --- | --- |
| API model id | `qwen3-8b-instruct` |
| Display name | `Qwen3 8B Instruct` |
| Source model | `Qwen/Qwen3-8B` |
| GGUF repo | `Qwen/Qwen3-8B-GGUF` |
| GGUF filename | `Qwen3-8B-Q4_K_M.gguf` |
| Quantization | `Q4_K_M` |
| License | `Apache-2.0` |
| Parameters | ~8.2B |
| Native context tokens | `32768` |
| Maximum validated context tokens | `131072` at model-card level; token.place validates only `65536` in this arc |
| Supported token.place tiers | `8k-fast`, `64k-full` |
| Default API v1 mode | non-thinking / non-reasoning |
| Context extension policy | no YaRN at or below native context; model-profile `factor=2.0` is a context multiplier for `64k-full` over original context `32768`; llama.cpp `--rope-scale 2` maps to llama-cpp-python `rope_freq_scale=0.5`, while `yarn_ext_factor` is a separate extrapolation-mix parameter and is not overridden |
| Chat-template policy | Qwen/GGUF Jinja template or verified Qwen llama-cpp-python handler; never `llama-3` |
| Compatibility alias plan | During P23d, accept `llama-3.1-8b-instruct` as an intentional transition alias to the active canonical Qwen profile, unless maintainers choose a tested hard cut |

The API id uses `instruct` because API v1 exposes an instruction-following chat
completion surface even though the upstream source model is named `Qwen3-8B`.

## Model-profile/catalog architecture

P23b should stop scattering model constants and introduce a single source of
truth for model metadata and artifact resolution. A profile/catalog abstraction
should include, at minimum:

- `profile_id` and canonical `api_model_id`.
- `aliases`, including whether an alias is public, invisible, deprecated, or a
  transition alias.
- `owner`, `provider`, and upstream `source_model`.
- GGUF repo/id, filename, download URL or Hugging Face repo+filename tuple, and
  canonical family URL.
- Expected quantization and license.
- Native context tokens and maximum validated context tokens.
- Supported token.place context tiers.
- Context-extension policy, including whether YaRN/RoPE is required for a tier.
- Chat-template policy, including exact allowed fallback behavior.
- Thinking/non-thinking policy.
- Generation defaults, e.g. Qwen non-thinking target sampling values if supported
  by the runtime validation path.
- Docs/display labels and concise operator-facing descriptions.

Profile use should be layered:

1. Config selects an active profile id, defaulting to Llama until P23d.
2. The selected profile resolves artifact metadata used by ModelManager and the
   desktop bridge.
3. API v1 model catalog exposes only profiles that are safe to advertise.
4. Request validation resolves aliases to canonical profile ids.
5. Relay scheduler routes by resolved model capability and context tier.
6. Compute nodes validate the decrypted request against their active profile and
   fail closed on model or tier mismatch.

## API compatibility policy

Recommended transition behavior:

- P23b keeps `llama-3.1-8b-instruct` canonical and does **not** alias it to Qwen.
- P23c makes Qwen runnable but still not default.
- P23d switches the canonical active API v1 model to `qwen3-8b-instruct`.
- During P23d, keep `llama-3.1-8b-instruct` accepted as a compatibility alias to
  the active Qwen profile for at least one transition period.
- `/api/v1/models` should eventually list only the canonical active public model,
  `qwen3-8b-instruct`, unless maintainers intentionally choose to expose both
  profiles. If both are exposed, scheduler semantics must be explicit and tested.
- API v1 request validation should accept old aliases only when alias mapping is
  intentional, documented, and tested.
- Existing invisible compatibility aliases such as OpenAI-style ids should keep
  mapping to the active canonical profile only if that is already current
  behavior and tests prove it.
- DSPACE should eventually request `qwen3-8b-instruct`; alias support avoids a
  hard cutover race while DSPACE staging follows token.place staging.

When a request uses an old alias after P23d, the resolved canonical model should
be Qwen. Scheduler filtering must then require Qwen-capable nodes, not stale
Llama-only nodes.

## Chat-template handling

Qwen must not be run through `chat_format='llama-3'`.

Implementation expectations for P23c:

- Prefer the GGUF/Jinja chat template embedded in the Qwen GGUF when exposed by
  llama-cpp-python.
- If the installed llama-cpp-python version exposes a Qwen/Qwen3 chat format or
  chat handler, use it only after verifying that it matches the Qwen3 template,
  supports non-thinking mode correctly, and is available in packaged desktop
  runtimes.
- If no correct template path exists, fail startup/warm-load with a clear error
  instead of silently running Qwen with Llama formatting.
- Context admission and generation must render/tokenize with the same template
  path. The exact admission check must count the prompt as the runtime will see
  it at generation time.
- Tests must fail if Qwen silently falls back to Llama formatting.
- Diagnostics may include safe template metadata such as `chat_template_mode`,
  but must not include rendered prompts, plaintext messages, or model output.

## Non-thinking mode and `<think>` leakage prevention

API v1 invariant: clients receive non-reasoning/non-thinking assistant content.
Qwen must be run with thinking disabled, and no `<think>` block may appear in API
v1 assistant content.

P23c should implement the strongest verified mechanism available in the current
llama-cpp-python/runtime stack:

- Preferred: use Qwen's chat template with `enable_thinking=False` or the
  runtime-specific equivalent if inspection confirms support.
- If the runtime supports only a soft prompt switch, inject a minimal `/no_think`
  control in the correct template position. This control must be runtime-internal
  and must not be exposed as DSPACE or user prompt text.
- If both a template kwarg and `/no_think` are needed for reliable local GGUF
  behavior, document why in code comments and tests.
- Do not guess parameter names. P23c must inspect the current llama-cpp-python
  API and encode the chosen integration in tests.

Validation policy:

- Add startup/model smoke coverage that asks a simple private-safe prompt such as
  `hi` and asserts the assistant response contains no `<think>` marker.
- Add response validation as a defensive fallback. Prefer rejecting unsafe output
  with a compute-node error such as `compute_node_invalid_model_output` during
  tests, unless product requirements explicitly choose sanitizer behavior.
- If a sanitizer is added, it is defense-in-depth only. Primary control must be
  template/runtime configuration, and reasoning content must never be forwarded
  to clients.

## YaRN/RoPE behavior for 64K

Definitions:

- **Model native context** is the context length the upstream model supports
  without extension. For Qwen3-8B, this is `32768` tokens.
- **token.place context tier** is the operator/runtime service profile exposed to
  clients and scheduler, currently `8k-fast` (`8192`) and `64k-full` (`65536`).
- **Exact context admission** is the compute-side post-decryption validation that
  renders/tokenizes the actual API v1 messages and verifies the request fits the
  active node's served context before generation.

Required behavior:

- `8k-fast`: no YaRN/RoPE scaling for Qwen, because `8192 <= 32768`.
- `64k-full`: apply YaRN/RoPE scaling for Qwen because `65536 > 32768`.
- Initial 64K settings: factor `2.0`, original/native context `32768`, target
  context `65536`, scaling type `yarn`.
- Llama behavior remains unchanged in P23c.
- 128K is out of scope; do not add a 128K tier or factor `4.0` in this arc.

ModelManager loading sequence should be explicit:

1. Resolve active context profile (`8k-fast` or `64k-full`) before the model is
   loaded.
2. Resolve active model profile.
3. Compare active context tokens to the model's native context tokens.
4. Apply profile-specific context-extension kwargs only when required and
   supported.
5. Fail warm-load/startup if Qwen `64k-full` requires YaRN/RoPE but the current
   llama-cpp-python version cannot accept/apply the needed settings.
6. Register node capabilities only after the runtime is actually loaded with the
   claimed model and context profile.

P23c must inspect current llama-cpp-python constructor fields before
implementation. The design intentionally does not hardcode unverified parameter
names as absolute truth. Tests should assert the actual kwargs used by the
installed/supported runtime wrapper.

## Desktop app migration

P23d should update desktop surfaces after P23b/P23c are merged and tested:

- **Download/inspect defaults:** the default inspect/download target becomes
  `Qwen3-8B-Q4_K_M.gguf` from `Qwen/Qwen3-8B-GGUF`.
- **Model file path behavior:** path resolution should remain profile-driven.
  The active profile's filename determines whether the active model exists.
- **Existing Llama file:** if an operator already downloaded the Llama GGUF, the
  app must not treat it as satisfying the Qwen profile. It should prompt for or
  download the Qwen GGUF.
- **File cleanup:** do not delete old Llama files automatically. They are useful
  for rollback and should remain operator-managed.
- **UI display:** show `Qwen3 8B Instruct`, Q4_K_M, and context tier information
  in operator-facing UI where model metadata is displayed.
- **Re-download:** users need the new Qwen GGUF unless it is already present at
  the resolved path. Error messages should make this clear.
- **Errors:** download and compatibility errors should name the expected Qwen
  filename/repo, current path, and the action needed, without exposing prompts,
  secrets, or decrypted traffic.
- **macOS/Windows packaging:** bundled sidecar dependencies must support the
  verified Qwen chat template path and YaRN/RoPE kwargs. GPU-capable
  llama-cpp-python builds remain release requirements for desktop GPU modes.
  Packaging must not silently fall back to CPU when GPU runtime support is
  expected.

## Landing page migration

P23d should update the landing chat only after `/api/v1/models` and Qwen runtime
support are ready:

- The model dropdown/default should select `qwen3-8b-instruct` when the catalog
  advertises it.
- `EMERGENCY_MODEL_FALLBACK_ID` should become `qwen3-8b-instruct`.
- Visible labels and descriptions should identify Qwen3 8B Q4_K_M.
- The landing page should continue to prefer `/api/v1/models` as the source of
  truth and use emergency fallback only when the catalog is unavailable.
- Auto/`8k-fast`/`64k-full` tier behavior remains unchanged.
- Landing chat remains API v1 and non-streaming; do not introduce API v2 or SSE
  paths as part of this migration.

## Relay and scheduler impact

The relay remains blind to plaintext and model internals:

- Relay-owned state may store ciphertext and safe routing metadata only.
- Safe metadata includes requested/resolved model id, context tier, node
  capability ids, health state, capacity state, and artifact/profile labels.
- Relay must not log or inspect plaintext prompts, responses, rendered templates,
  decrypted payloads, or tool arguments.

Scheduler behavior:

- Nodes register active model capability after warm-load using canonical model ids
  and supported aliases/capabilities as defined by the profile catalog.
- Scheduler filters by resolved requested model and requested context tier.
- Old aliases must map consistently. If `llama-3.1-8b-instruct` resolves to
  Qwen in P23d, the scheduler must route only to Qwen-capable nodes.
- Mixed Llama/Qwen rollout needs explicit behavior:
  - A stale Llama node should not satisfy a canonical Qwen request.
  - An old-id request that aliases to Qwen should also not be routed to a stale
    Llama node.
  - If maintainers choose to expose both Llama and Qwen as distinct profiles,
    clients requesting Llama should route only to Llama-capable nodes and clients
    requesting Qwen should route only to Qwen-capable nodes.
- Compute nodes must revalidate the decrypted request against their active model
  and context profile and fail closed on mismatch.

## DSPACE impact and follow-up

DSPACE migration is a downstream P23e task after token.place P23d is proven in
staging:

- DSPACE's default token.place API v1 model id should move from
  `llama-3.1-8b-instruct` to `qwen3-8b-instruct`.
- DSPACE environment/runtime override support must continue to work, including a
  rollback override back to `llama-3.1-8b-instruct` if needed.
- If token.place preserves the old id as a transition alias, DSPACE can migrate
  after token.place staging without a hard cutover race.
- DSPACE prompt improvements from P16-P22 should remain provider/model-agnostic;
  the model id swap should not change prompt planner, RAG, token-lite, save/game
  schemas, or OpenAI provider behavior.
- DSPACE staging should verify ordinary chat prompts, domain prompts, and no
  `<think>` leakage before production rollout.

## Testing plan

### P23b model-profile plumbing tests

- API model catalog tests prove Llama default output remains backward compatible.
- Qwen profile metadata tests assert exact id, display name, source model, GGUF
  repo, filename, quantization, license, native context, max context, supported
  tiers, thinking policy, and context-extension policy.
- Alias tests prove old Llama aliases still map to Llama in P23b and do not map
  to Qwen yet.
- ModelManager artifact metadata tests prove metadata is profile-sourced while
  preserving legacy keys.
- Desktop bridge fallback tests prove defaults remain Llama and existing env
  overrides still win.
- API v1 catalog tests lock the conservative decision not to advertise Qwen as
  selectable until runtime support exists, if that decision is chosen.

### P23c Qwen runtime tests

- Mock llama_cpp runtime initialization asserts Qwen omits
  `chat_format='llama-3'`.
- Qwen template tests assert the verified GGUF/Jinja or Qwen handler path is used.
- Tests fail if Qwen falls back to Llama formatting.
- Context admission tests prove render/tokenize uses the same template mode as
  generation.
- Non-thinking tests prove template kwargs or `/no_think` injection are applied.
- Output validation tests prove responses containing `<think>` are rejected or
  safely handled and never forwarded as assistant content.
- Qwen `8k-fast` tests prove no YaRN/RoPE scaling is enabled.
- Qwen `64k-full` tests prove the model-profile YaRN/RoPE context multiplier `2.0`, original context `32768`,
  target context `65536`, llama-cpp-python `rope_freq_scale=0.5`, no `yarn_ext_factor` override, and safe diagnostics are configured.
- Boundary admission tests cover exact `64k-full` edges at `65535` and `65536`
  tokens to catch llama-cpp-python YaRN/RoPE rounding, truncation, or off-by-one
  behavior at the effective tier limit.
- Llama `8k-fast`/`64k-full` tests prove existing behavior remains unchanged.
- Warm-load failure tests prove Qwen `64k-full` fails clearly when YaRN/RoPE is
  unsupported.
- Diagnostics tests prove active model/template/context metadata is included and
  plaintext/ciphertext payload content is not.

### P23d default-switch tests

- API v1 catalog lists `qwen3-8b-instruct` as the primary/default public model.
- Old Llama id compatibility behavior is explicit and tested.
- Desktop bridge fallback reports Qwen defaults.
- ModelManager defaults resolve to Qwen filename, URL/repo, profile id, and API
  model id.
- Landing page emergency fallback id and visible labels are Qwen.
- Relay model filtering resolves Qwen canonical id and old alias correctly.
- Mixed node tests prove Qwen requests do not route to stale Llama nodes.
- API v2 expectations remain unchanged.
- Active default docs/tests remove hardcoded Llama references while historical
  release notes remain accurate.

### Integration, desktop, landing, and staging tests

- API v1 desktop bridge E2EE integration continues to pass without plaintext
  relay state.
- Desktop inspect/download tests cover missing Qwen file, existing Llama file,
  Qwen download path, and clear errors.
- Landing model dropdown tests cover catalog-driven Qwen default and fallback.
- Exact admission tests cover near-8K, near-32K, near-64K, and exact `65535` /
  `65536` token prompts with safe synthetic/private-safe text.
- Manual staging smoke prompts for token.place and DSPACE:
  - `hi`
  - `do I have enough green PLA?`
  - `I’d like to make a 3D printed rocket and 10 benchies. Is it enough for that?`
  - `where do I import a gamesave?`
  - known 64K-ish synthetic/private-safe prompt
- Manual smoke records requested tier, selected tier, prompt tokens, latency,
  active model id, template mode, YaRN status, and no `<think>` leakage.

## Rollout plan

1. Merge P23b with Llama still default.
   - Verify profile catalog and metadata are centralized.
   - Confirm Qwen metadata exists but is not advertised/runnable by default.
2. Merge P23c with Llama still default.
   - Verify explicit Qwen opt-in runtime works in tests.
   - Verify Qwen template, non-thinking, and 64K YaRN/RoPE behavior.
3. Deploy P23c to non-production if possible with explicit Qwen operator tests.
4. Merge P23d and switch token.place staging default to Qwen.
   - Desktop staging operators download/load Qwen.
   - `/api/v1/models` shows Qwen as the canonical active public model.
   - Landing chat succeeds for Auto, `8k-fast`, and `64k-full` where capacity is
     available.
   - Old Llama id requests alias safely or fail clearly according to the tested
     policy.
5. Monitor operational logs/diagnostics for safe metadata only:
   - active profile id and API model id,
   - display name,
   - GGUF filename,
   - quantization,
   - context tier and `n_ctx`,
   - native context,
   - chat template mode,
   - thinking disabled/enforced,
   - YaRN/RoPE enabled boolean, context multiplier factor, derived `rope_freq_scale`, original context,
   - backend/offload class.
6. After token.place staging is healthy, merge P23e in DSPACE.
7. Add P23f ad-hoc smoke script and rollback runbook.

## Rollback plan

Rollback must be a profile/config switch, not an emergency code rewrite:

- Keep both Qwen and Llama profile definitions available.
- Keep both GGUF files in place where already downloaded; do not delete old Llama
  files automatically during migration.
- Switch the default active profile back to Llama through the supported env/config
  path once P23b/P23d provide it.
- Restart desktop operators so ModelManager constructs the Llama runtime with the
  Llama profile and context tier.
- Confirm `/api/v1/models` and relay node capabilities reflect the rollback
  model id and context tiers.
- If DSPACE has already moved to Qwen, set its runtime override back to
  `llama-3.1-8b-instruct` until token.place is switched forward again.
- Verify landing chat and DSPACE smoke prompts still succeed and contain no
  `<think>` leakage.
- Confirm diagnostics do not contain plaintext prompts/responses during rollback.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Wrong chat template | Poor output quality, broken turn formatting, possible context-count mismatch | Never run Qwen with `chat_format='llama-3'`; inspect llama-cpp-python support; tests fail on Llama fallback; align context admission and generation templates. |
| Thinking output leakage | API v1 clients receive hidden reasoning or `<think>` blocks | Force non-thinking via verified template/runtime mechanism; add smoke test; add defensive response validation; reject/sanitize unsafe output without forwarding reasoning. |
| YaRN not applied at 64K | Qwen 64K nodes falsely advertise a context they cannot serve reliably | Apply Qwen YaRN/RoPE only when active context exceeds native 32K; fail warm-load if unsupported; expose safe diagnostics. |
| YaRN degrades 8K | Short-context latency/quality regression | Do not apply YaRN/RoPE for `8k-fast`; test that Qwen 8K has no scaling kwargs. |
| GGUF download mismatch | Operators load wrong model or cannot load expected file | Profile-driven filename/repo/url; metadata tests; desktop inspect/download checks; clear errors naming expected Qwen artifact. |
| Mixed Llama/Qwen node support during rollout | Requests route to incompatible stale nodes | Resolve aliases before scheduling; register active canonical capabilities after warm-load; compute nodes fail closed on mismatch. |
| Stale desktop app bundle | Operators see or download old Llama default | Update desktop labels/fallback metadata in P23d; require reinstall/restart in staging checklist; keep old file but do not treat it as Qwen. |
| DSPACE requesting old model id | DSPACE fails during cutover | Keep old id as transition alias in token.place or document hard-cut behavior; update DSPACE only after staging; preserve env override. |
| Benchmarks do not translate to DSPACE quality | Model switch passes synthetic tests but regresses app-specific answers | Run DSPACE smoke prompts after token.place staging; keep rollback path; keep prompt improvements provider/model-agnostic. |
| llama-cpp-python API drift | Unverified kwargs break packaged runtime | Inspect installed APIs in P23c; encode decisions in tests; fail fast with clear diagnostics when unsupported. |
| Relay diagnostic overexposure | E2EE invariant violation | Restrict diagnostics to safe metadata; do not log rendered prompts, decrypted payloads, or model output. |

## Acceptance checklist for P23a

- [x] Complete migration map from current Llama touchpoints to target Qwen profile.
- [x] Model-profile architecture proposed for P23b.
- [x] API compatibility and alias plan defined.
- [x] Qwen chat-template requirements explicitly forbid Llama formatting.
- [x] API v1 non-thinking invariant and no-`<think>` behavior documented.
- [x] 64K YaRN/RoPE behavior documented with factor `2.0` over native `32768`.
- [x] Desktop, landing page, relay/scheduler, and DSPACE impacts covered.
- [x] Unit, integration, desktop, landing, staging, rollout, and rollback plans
  included.
- [x] API v2 explicitly left untouched.
- [x] No runtime behavior changes are made by this design document.
