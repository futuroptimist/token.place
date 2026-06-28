# Qwen3 API v1 model migration design

Status: design for P23a. This document is an implementation map for P23b-P23e and
intentionally makes no runtime, API, desktop, landing-page, API v2, or DSPACE code
changes.

## Goals and non-goals

### Goals

- Switch the canonical token.place API v1 desktop compute model from
  `Meta-Llama-3.1-8B-Instruct-Q4_K_M` to `Qwen3-8B-Q4_K_M` in a controlled
  sequence.
- Preserve API v1 behavior: OpenAI-compatible, non-streaming chat completions,
  non-reasoning/non-thinking assistant output, and existing 8K/64K context tier
  semantics.
- Preserve relay-blind E2EE: relay-owned state, logs, diagnostics, and scheduling
  metadata must never contain plaintext prompts, responses, tool arguments, or
  model output.
- Preserve DSPACE compatibility during rollout by keeping old model IDs accepted
  through an intentional, tested alias period.
- Centralize model metadata and runtime artifact settings so future model-family
  changes are profile changes rather than scattered constant edits.

### Non-goals

- Do not change API v2. API v2 remains incomplete and out of the active runtime
  path for this migration.
- Do not add streaming to API v1.
- Do not add multi-model-per-node serving in this arc.
- Do not implement 128K context support in this arc.
- Do not change DSPACE until the token.place staging migration is proven.

## Migration sequence

1. **P23a:** land this design document only.
2. **P23b:** add model-profile/catalog/config plumbing and Qwen metadata while
   leaving the Llama profile as the default.
3. **P23c:** make Qwen runnable through API v1 by implementing the correct chat
   template path, forced non-thinking behavior, and 64K YaRN/RoPE support while
   leaving Llama as default.
4. **P23d:** switch token.place defaults, desktop app, landing page, docs, and
   tests to Qwen.
5. **P23e:** update DSPACE to request the new canonical token.place API v1 model
   after token.place staging is healthy.

## Current hardcoded Llama touchpoints

P23b-P23d should audit and migrate the following surfaces. The checklist is
intentionally broader than the first plumbing PR so later prompts can remove
active default Llama assumptions without rewriting history in release notes.

- [ ] `api/v1/models.py`
  - Current canonical API v1 model ID and OpenAI-compatible model object shape.
  - Alias behavior for OpenAI-compatible model IDs.
  - `/api/v1/models/{model_id}` lookup and request validation paths.
- [ ] `utils/config_schema.py`
  - Default model filename, URL, model family URL, context sizing, and any typed
    config schema fields that implicitly assume Llama.
- [ ] `utils/llm/model_manager.py`
  - Default GGUF filename and download URL.
  - Model artifact metadata returned to desktop inspect/download flows.
  - llama.cpp initialization defaults, especially `chat_format='llama-3'`.
  - Exact context-admission render/tokenize path.
  - Startup/diagnostic metadata that identifies active model/template/context.
- [ ] `utils/networking/relay_client.py`
  - Compute-node registration metadata.
  - Requested model validation, alias resolution, and context-tier routing.
  - Error behavior for unsupported model/context combinations.
- [ ] `desktop-tauri/src-tauri/python/model_bridge.py`
  - Fallback model metadata used when shared Python imports are unavailable.
  - Download/inspect behavior and environment overrides for filename, URL, and
    family URL.
- [ ] Desktop Tauri React/Rust files that show model names/download state
  - Operator-facing labels, download prompts, progress state, inspect output,
    error copy, and bundled defaults.
  - macOS/Windows package paths and sidecar-launch assumptions.
- [ ] `static/chat.js`
  - `EMERGENCY_MODEL_FALLBACK_ID`.
  - Selected model initialization, catalog fallback behavior, and error copy.
- [ ] `static/index.html`
  - Static API examples, visible labels, and model metadata descriptions.
- [ ] Release docs, smoke scripts, promotion docs, and tests
  - Current-behavior docs should describe Qwen after P23d.
  - Historical release notes should remain historically accurate.
  - Smoke scripts should verify the active default without exposing plaintext.
- [ ] DSPACE runtime model default as a downstream follow-up
  - DSPACE should move from `llama-3.1-8b-instruct` to `qwen3-8b-instruct` in
    P23e, after token.place preserves compatibility and staging is proven.

## Target Qwen model profile

The target profile should be available centrally before it becomes default.

| Field | Value |
| --- | --- |
| API model ID | `qwen3-8b-instruct` |
| Display name | `Qwen3 8B Instruct` |
| Source model | `Qwen/Qwen3-8B` |
| GGUF repo | `Qwen/Qwen3-8B-GGUF` |
| GGUF filename | `Qwen3-8B-Q4_K_M.gguf` |
| Parameters | approximately 8.2B |
| Quantization | `Q4_K_M` |
| License | Apache-2.0 |
| Native context | 32,768 tokens |
| token.place tiers | `8k-fast`, `64k-full` |
| Default API v1 mode | non-thinking / non-reasoning |
| 64K context extension | YaRN/RoPE factor 2.0 over original context 32,768 |
| 128K status | explicitly out of scope for this arc |

Compatibility alias plan:

- During the P23d transition, keep `llama-3.1-8b-instruct` accepted as an
  intentional compatibility alias to the active default Qwen profile unless
  maintainers explicitly choose a hard cut.
- Alias mapping must be tested and scheduler-visible after resolution; old IDs
  must not accidentally route Qwen requests to stale Llama nodes.
- DSPACE should eventually request `qwen3-8b-instruct` directly, but alias support
  avoids a hard cutover race while deployments roll forward.

## Model-profile architecture

token.place should stop scattering model constants and introduce a model
profile/catalog abstraction. The active profile should be the single source of
truth for API catalog metadata, desktop download metadata, ModelManager artifact
metadata, runtime initialization policy, and safe diagnostics.

A profile should include at least:

- canonical API model ID;
- compatibility aliases;
- owner/provider/source model;
- GGUF repo or repo ID;
- GGUF filename;
- download URL or Hugging Face repo plus filename;
- canonical family/model page URL;
- expected quantization;
- license;
- native context tokens;
- maximum validated context tokens;
- supported token.place context tiers;
- default context tokens;
- context-extension policy, including when YaRN/RoPE is required;
- chat-template policy;
- non-thinking/thinking policy;
- profile-driven generation defaults;
- docs/display labels and descriptions.

The profile layer should support at least two phases:

1. **Infrastructure phase:** Llama remains default; Qwen exists internally and is
   test-covered but not advertised as runnable unless runtime support is ready.
2. **Default-switch phase:** Qwen becomes default; Llama remains a historical or
   rollback profile, and selected old IDs become compatibility aliases only when
   intentionally mapped and tested.

## API compatibility

API v1 must remain OpenAI-compatible and non-streaming. The default model can
change without changing the API envelope.

Recommended compatibility behavior:

- Keep `llama-3.1-8b-instruct` accepted as a compatibility alias during the
  transition.
- Keep existing invisible OpenAI-style aliases, such as compatibility IDs used by
  DSPACE or smoke tests, mapped to the active API v1 profile if that is current
  behavior.
- `/api/v1/models` should eventually list only the canonical active model
  (`qwen3-8b-instruct`) unless maintainers choose to expose both canonical
  profiles.
- API v1 request validation should accept old aliases only if the mapping is
  explicit, documented, and tested.
- `/api/v1/models/{model_id}` should resolve aliases consistently with chat
  completion validation and relay scheduling.

## Chat-template handling

Qwen must not run through `chat_format='llama-3'`.

Implementation requirements for P23c:

- Prefer the GGUF/Jinja chat template path when llama-cpp-python can use it
  correctly.
- If llama-cpp-python exposes a verified Qwen/Qwen3 chat format or chat handler,
  use that only after inspecting the installed API and confirming it matches the
  Qwen3 GGUF template semantics.
- Context admission and generation must render/tokenize through the same template
  path. Admission must not use a Llama fallback formatter for Qwen.
- If the correct Qwen template path is unavailable, fail fast at runtime startup
  or warm-load with a clear operator error rather than serving with the wrong
  prompt format.
- Tests must fail if Qwen silently falls back to Llama formatting.

## Non-thinking mode

API v1 invariant: token.place API v1 is non-reasoning/non-thinking. Qwen must be
configured so assistant content returned through API v1 never includes `<think>`
blocks.

Implementation requirements for P23c:

- Primary control must be runtime/template configuration, not response cleanup.
- Inspect llama-cpp-python and the active Qwen template before choosing the exact
  mechanism. Candidate mechanisms include `enable_thinking=False`, a Qwen Jinja
  template kwarg, `/no_think` in the correct template-controlled position, or a
  runtime-specific equivalent.
- Document the chosen mechanism in code comments/tests after inspection.
- Add a startup/model smoke test that asks a simple prompt, such as `hi`, and
  asserts no `<think>` appears in assistant content.
- Add response validation or a sanitizer only as a defensive fallback. The
  preferred safe behavior is to reject invalid model output with an error such as
  `compute_node_invalid_model_output` rather than leaking reasoning content.
- No relay-owned logs, diagnostics, or errors may include plaintext prompt or
  response text while enforcing this invariant.

## YaRN/RoPE behavior for 64K

There are three related but distinct quantities:

- **Model native context:** the context window the model supports without
  extension. For Qwen3-8B, this is 32,768 tokens.
- **token.place context tier:** the product-level tier requested by the user or
  scheduler, such as `8k-fast` or `64k-full`.
- **Exact context admission:** the pre-generation check that renders and tokenizes
  the actual request and admits or rejects it based on the active tier budget.

Required behavior:

- `8k-fast`: do not apply YaRN/RoPE scaling for Qwen. Static YaRN at 8K may
  degrade short-context behavior and must be avoided.
- `64k-full`: apply YaRN/RoPE scaling for Qwen because 65,536 exceeds native
  32,768.
- For 64K, use factor `2.0` against original/native context `32768`.
- Do not implement 128K in this arc. Future 128K work would use a factor such as
  `4.0` against the native 32,768 context only after separate validation.
- Do not apply Qwen YaRN/RoPE settings to Llama.
- Do not register a 64K Qwen node as available if the runtime cannot apply the
  required scaling.

ModelManager must know the active context profile before loading the model so it
can decide whether runtime constructor settings require context extension. The
implementation PR must inspect the current llama-cpp-python constructor and test
support for the exact field names before coding them as runtime kwargs. The
design expectation is to pass a scaling type equivalent to YaRN, an original
context of 32,768, and a factor of 2.0 when Qwen runs above native context, but
this document intentionally does not hardcode unverified parameter names as
absolute truth.

## Desktop app migration

P23d should update desktop operator surfaces after P23b/P23c have landed.

- Download and inspect defaults should resolve from the active model profile.
- The default model file path should point to `Qwen3-8B-Q4_K_M.gguf` after the
  default switch.
- If an operator already downloaded the old Llama GGUF, the app must not treat it
  as satisfying the Qwen default. It should prompt/download Qwen instead.
- The old Llama file should be left in place. Automatic deletion risks expensive
  re-downloads, rollback friction, and accidental user data loss.
- UI labels should display `Qwen3 8B Instruct` and make the Q4_K_M artifact clear
  where model download/inspect details are shown.
- Operators must re-download the new GGUF unless it is already present at the
  resolved Qwen filename/path.
- Download errors should name the Qwen filename/repo and explain common
  compatibility issues, such as missing network access, insufficient disk space,
  or unsupported local runtime/GPU build.
- macOS and Windows packaging must preserve sidecar access to the Python bridge,
  model directory resolution, and GPU-capable llama-cpp-python release
  requirements. GPU mode must not silently fall back to CPU when GPU runtime
  support is expected.

## Landing page migration

The landing page should continue to depend on `/api/v1/models` where possible.
P23d should update:

- model dropdown/default selection to prefer the canonical Qwen model returned by
  the catalog;
- `EMERGENCY_MODEL_FALLBACK_ID` to `qwen3-8b-instruct`;
- visible model labels/descriptions and static API examples;
- model metadata display for Qwen profile details;
- tests that currently expect Llama defaults.

Auto, `8k-fast`, and `64k-full` tier behavior remains unchanged. The landing page
must not introduce API v2, streaming, or plaintext relay diagnostics.

## Relay and scheduler impact

The relay remains blind to plaintext and model internals. It may use safe routing
metadata such as model ID, context tier, node health, and capability flags, but it
must not inspect, log, or persist plaintext model payloads.

Required behavior:

- Compute nodes register the active resolved model capability and supported
  context tiers.
- Scheduler matching routes by resolved requested model and context tier.
- Alias mapping must happen consistently before scheduler matching.
- During mixed Llama/Qwen rollout, stale Llama nodes must not receive Qwen
  requests unless maintainers intentionally declare them compatible. For this
  migration, old Llama request IDs should resolve to the current default Qwen
  profile only after P23d, and scheduler matching should then require Qwen-capable
  nodes.
- Error responses for no matching model/tier should remain clear and safe, using
  codes such as `no_matching_compute_node`, `compute_node_model_unsupported`, or
  `compute_node_context_tier_unsupported` as appropriate.

## DSPACE impact

DSPACE migration is a downstream P23e follow-up.

- DSPACE's default token.place model ID should move to `qwen3-8b-instruct` after
  token.place staging with P23d is healthy.
- DSPACE environment/runtime override support must continue to work so staging or
  production can force `llama-3.1-8b-instruct` during rollback.
- If token.place preserves the old ID as a compatibility alias, DSPACE can
  migrate after token.place without a hard cutover race.
- DSPACE prompt improvements from P16-P22 should remain provider/model-agnostic.
  The token.place model swap must not change DSPACE planner, RAG, token-lite,
  OpenAI provider, persona, save/game schema, or prompt-shaping behavior.

## Testing plan

### Unit tests

- API model catalog lists the expected default for each phase.
- API model object metadata includes profile-driven fields without breaking the
  OpenAI-compatible shape.
- Alias tests verify old Llama IDs, invisible OpenAI-style aliases, and Qwen IDs
  resolve exactly as intended for each phase.
- Model profile tests assert exact Qwen metadata: ID, display name, source model,
  GGUF repo, filename, quantization, license, native context, max validated
  context, supported tiers, non-thinking policy, and 64K YaRN/RoPE policy.
- ModelManager metadata tests verify artifact metadata comes from the active
  profile and preserves backward-compatible keys.
- Runtime initialization tests with mock `llama_cpp` verify Qwen never passes
  `chat_format='llama-3'`.
- Template tests fail if Qwen context admission uses a Llama formatter fallback.
- Non-thinking tests assert template/runtime controls are applied and generated
  `<think>` content is rejected or safely handled without leaking.
- Context tests assert 8K Qwen has no YaRN/RoPE scaling and 64K Qwen uses factor
  2.0 with original context 32,768.
- Llama regression tests assert current behavior remains unchanged until P23d and
  rollback profile behavior remains available after P23d.

### Integration tests

- API v1 desktop bridge E2EE tests continue to pass without plaintext relay
  exposure.
- Relay-client tests verify node registration advertises safe model/profile/tier
  metadata.
- Scheduler tests verify Qwen canonical ID and old aliases route only to
  compatible Qwen nodes after alias resolution.
- Exact admission tests verify rendered/tokenized prompt length is computed with
  the same template path used for generation.
- Warm-load tests verify Qwen 64K fails safely if required YaRN/RoPE runtime
  support is unavailable.

### Desktop tests

- Inspect/download tests verify default metadata before and after P23d.
- Environment override tests preserve
  `TOKEN_PLACE_DEFAULT_MODEL_FILENAME`, `TOKEN_PLACE_DEFAULT_MODEL_URL`, and
  `TOKEN_PLACE_DEFAULT_MODEL_FAMILY_URL` behavior.
- Tests verify an existing Llama file does not satisfy the Qwen default path.
- Packaging smoke tests verify safe metadata and GPU diagnostics without prompt
  text.

### Landing tests

- Model dropdown tests verify Qwen is selected from `/api/v1/models` after P23d.
- Emergency fallback tests verify `qwen3-8b-instruct` after P23d.
- Auto/8K/64K UI tests verify tier behavior remains unchanged.
- Static docs/examples tests update current API v1 examples while preserving
  historical docs.

### Manual staging tests

- `curl /api/v1/models` shows Qwen as canonical active model after P23d.
- Landing page Auto + `hi` succeeds and returns no `<think>`.
- Landing page `64k-full` with a known large prompt succeeds when within exact
  admission limits.
- Desktop 8K operator inspect/download/load shows Qwen, non-thinking diagnostics,
  and no YaRN/RoPE.
- Desktop 64K operator inspect/download/load shows Qwen and YaRN/RoPE factor 2.0
  over original context 32,768.
- DSPACE smoke prompts after P23e:
  - `hi`
  - `do I have enough green PLA?`
  - `where do I import a gamesave?`
  - no `<think>` output appears.

## Rollout plan

1. Land P23b profile/catalog infrastructure with Llama default unchanged.
2. Land P23c Qwen runtime support and verify Qwen works explicitly in tests and
   manual local smoke where a GGUF is available.
3. Land P23d and deploy token.place staging with Qwen as the default.
4. Verify operational logs/diagnostics contain safe metadata only:
   - active API model ID;
   - profile ID/display name;
   - GGUF filename and quantization;
   - chat template mode;
   - thinking disabled/enforced;
   - context tier and `n_ctx`;
   - native context;
   - YaRN/RoPE enabled boolean, factor, and original context;
   - backend/offload diagnostics.
5. Run landing and desktop staging smoke tests for 8K and 64K.
6. Update DSPACE in P23e after token.place staging is healthy.
7. Promote with explicit release notes and rollback instructions.

## Rollback plan

- Switch the active token.place default profile back to Llama through the
  profile/config mechanism introduced in P23b/P23d.
- Keep both GGUF files on disk. Do not delete Qwen or Llama automatically.
- Restart desktop operators or relay/compute nodes so they re-register the active
  profile and context capabilities.
- Force DSPACE back to `llama-3.1-8b-instruct` with its runtime environment
  override if P23e has already deployed.
- Verify rollback with:
  - `/api/v1/models` active/default catalog response;
  - relay diagnostics showing safe active model/profile metadata;
  - desktop inspect/load showing Llama default again;
  - landing `hi` smoke with no `<think>` and no plaintext relay logs;
  - scheduler tests proving Qwen requests no longer route to Llama nodes unless a
    deliberate alias policy says so.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Wrong chat template | Do not run Qwen with `chat_format='llama-3'`; inspect llama-cpp-python support; fail fast if the Qwen template path is unavailable; add template fallback tests. |
| Thinking output leakage | Force non-thinking through runtime/template configuration; add startup smoke tests; reject or defensively sanitize `<think>` only as fallback; never log plaintext. |
| YaRN not applied at 64K | Profile policy requires YaRN/RoPE above native context; add runtime kwarg tests and warm-load failure when unsupported; expose safe diagnostics. |
| YaRN degrading 8K | Apply scaling only when active context exceeds native 32K; add 8K no-YaRN tests. |
| GGUF download mismatch | Centralize filename/repo/URL in profile metadata; verify desktop fallback and ModelManager artifact metadata; include checksum or size validation if later added. |
| Mixed node model support during rollout | Resolve aliases before scheduler matching; require active Qwen capability for resolved Qwen requests; keep stale Llama nodes from satisfying Qwen traffic. |
| Stale desktop app bundle | Update UI labels and Python bridge fallback together; require desktop rebuild/reinstall in staging checklist; preserve clear errors. |
| DSPACE requesting old model ID | Keep old ID as a tested compatibility alias during transition; update DSPACE default in P23e; preserve env rollback override. |
| Benchmarks not translating to DSPACE quality | Run DSPACE smoke prompts after token.place staging; keep P16-P22 prompt improvements model-agnostic; collect manual quality notes before production promotion. |
| API v2 accidental changes | Keep all implementation prompts scoped to API v1; add no API v2 routing or catalog behavior changes. |
| Relay plaintext leakage while debugging | Restrict diagnostics to safe routing/model metadata; no prompt/response text in relay logs or state. |

## Acceptance checklist for implementation PRs

- [ ] P23b centralizes profiles and adds test-covered Qwen metadata without
  changing the Llama default.
- [ ] P23c makes Qwen runnable with correct template, non-thinking enforcement,
  and 64K YaRN/RoPE support without changing the default.
- [ ] P23d switches token.place API v1, desktop, landing, and docs to Qwen with
  intentional compatibility aliases and rollback path.
- [ ] P23e updates DSPACE to request `qwen3-8b-instruct` by default while
  preserving runtime overrides.
- [ ] No PR in this arc touches API v2 runtime routing.
- [ ] No relay-owned state/log/diagnostic path exposes plaintext model payloads.
