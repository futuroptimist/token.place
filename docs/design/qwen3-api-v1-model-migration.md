# Qwen3 8B Q4_K_M API v1 model migration design

Status: design proposal for the P23 model-family migration sequence. This
proposal is intentionally documentation-only: it does not change runtime code,
API behavior, desktop UI, landing-page defaults, generated artifacts, API v2, or
DSPACE.

## Scope and non-goals

### In scope

- Design the API v1 default desktop compute model migration from
  `Meta-Llama-3.1-8B-Instruct-Q4_K_M` to `Qwen3-8B-Q4_K_M`.
- Preserve the current 8B-ish local desktop model class and `Q4_K_M`
  quantization target.
- Preserve API v1 semantics: OpenAI-compatible chat completions,
  non-streaming responses, relay-blind E2EE, and non-reasoning/non-thinking
  assistant content.
- Preserve the existing `8k-fast` and `64k-full` context-tier model.
- Capture the implementation sequence for P23b-P23e without changing behavior in
  this PR.

### Explicit non-goals

- No runtime behavior change in P23a.
- No default model switch in P23a.
- No desktop UI or landing-page copy change in P23a.
- No generated artifacts or downloaded model files in P23a.
- No API v2 changes. API v2 remains incomplete and must not receive active
  runtime traffic during this migration.
- No DSPACE code changes in token.place P23a-P23d. DSPACE follows in P23e after
  token.place staging proves the Qwen path.
- No API v1 streaming, no legacy relay endpoint revival, and no multi-model
  serving per compute node in this arc.

## Hard invariants

1. **API v1 remains non-streaming.** Responses are returned only after complete
   model generation. The migration must not add SSE or partial-token streaming to
   API v1 relay/client-server paths.
2. **Relay-blind E2EE remains mandatory.** Relay-owned state, queues, logs,
   diagnostics, and payloads may contain ciphertext and safe routing metadata
   only. They must never contain plaintext prompts, chat messages, tool
   arguments, model output, hidden thinking content, exact decrypted prompt token
   counts, or decrypted payload fragments.
3. **API v1 remains non-thinking.** Qwen3 supports thinking and non-thinking
   behavior, but token.place API v1 exposes a non-reasoning chat completion
   surface. No `<think>` block or hidden reasoning equivalent may be returned in
   assistant content.
4. **Context admission stays compute-owned.** The relay cannot inspect encrypted
   request content. The compute node must decrypt, render with the same chat
   template used for generation, tokenize with the active runtime tokenizer, and
   fail closed on context overflow.
5. **Runtime context is selected before model load.** The active context profile
   (`8k-fast` or `64k-full`) must be known before the in-process
   `llama_cpp.Llama` runtime is constructed.
6. **Qwen must not use a Llama chat template.** Running Qwen through
   `chat_format='llama-3'` is a correctness bug and must fail tests.

## Source facts and references

The implementation PRs should verify these facts against the current upstream
artifacts at implementation time, because llama.cpp and llama-cpp-python support
changes frequently.

- Qwen's Qwen3 announcement lists Qwen3-8B among the dense open-weight models
  and says the Qwen3 family is available under Apache 2.0.
  <https://qwenlm.github.io/blog/qwen3/>
- The `Qwen/Qwen3-8B-GGUF` Hugging Face repository is the intended GGUF source
  for `Qwen3-8B-Q4_K_M.gguf`.
  <https://huggingface.co/Qwen/Qwen3-8B-GGUF>
- The Qwen3-8B GGUF ecosystem documents 8.2B parameters, 32,768 native context,
  and 131,072-token extended context with YaRN. token.place only adopts the
  64K subset in this migration.
  <https://huggingface.co/unsloth/Qwen3-8B-GGUF>
- The `Qwen3-8B-Q4_K_M.gguf` artifact page shows llama-cpp-python usage via
  `Llama.from_pretrained(repo_id="Qwen/Qwen3-8B-GGUF", filename="Qwen3-8B-Q4_K_M.gguf")`.
  <https://huggingface.co/Qwen/Qwen3-8B-GGUF/blob/main/Qwen3-8B-Q4_K_M.gguf>

## Current hardcoded Llama touchpoints

P23b-P23d should start with a repo audit and classify each match as active
runtime behavior, UI/metadata, test expectation, historical docs, or downstream
follow-up. Historical release notes should stay historically accurate; active
surfaces should migrate.

### Repo-audit checklist

- [ ] `api/v1/models.py`
  - Current canonical API v1 launch model id is `llama-3.1-8b-instruct`.
  - Compatibility aliases route `llama-3-8b-instruct`, `gpt-3.5-turbo`, and
    `gpt-5-chat-latest` to the fixed launch model.
  - Public model metadata names Meta Llama 3.1, the Llama GGUF URL, and
    `Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf`.
- [ ] `utils/config_schema.py`
  - Model defaults and typed config fields may encode filename, URL, family URL,
    context size, or llama-specific runtime defaults.
- [ ] `utils/llm/model_manager.py`
  - Download URL, default filename, canonical family URL, artifact metadata, and
    runtime constructor defaults should be audited.
  - Current llama.cpp runtime behavior defaults to `chat_format='llama-3'`; this
    must remain only for Llama and must not be used for Qwen.
- [ ] `utils/networking/relay_client.py`
  - Capability registration, model id normalization, context-tier routing,
    diagnostics, and alias handling must resolve the requested model consistently
    before relay scheduling.
- [ ] `desktop-tauri/src-tauri/python/model_bridge.py`
  - Fallback inspect/download metadata and environment override handling must
    move from scattered Llama constants to the active model profile.
- [ ] Desktop Tauri React and Rust files that show model names, download state,
  active model metadata, or load errors.
  - Audit `desktop-tauri/src/**`, `desktop-tauri/src-tauri/src/**`, and related
    tests for visible Llama copy, filename checks, and stale default assumptions.
- [ ] `static/chat.js`
  - Audit emergency fallback model id, model dropdown population, default model
    selection, `/api/v1/models` assumptions, context-tier selection, and
    non-streaming API v1 flow.
- [ ] `static/index.html`
  - Audit visible labels, descriptions, and no-JS/fallback copy.
- [ ] Release docs, smoke scripts, promotion docs, and tests.
  - Active promotion/current-operations docs should move to Qwen in P23d.
  - Historical release notes should not be rewritten except when they describe
    current behavior rather than history.
- [ ] DSPACE runtime model default.
  - This is a downstream P23e follow-up in `democratizedspace/dspace`, not a
    token.place P23a-P23d code change.

## Target model profile

The intended Qwen profile should be represented centrally before becoming the
runtime default.

| Field | Value |
| --- | --- |
| API model id | `qwen3-8b-instruct` |
| Display name | `Qwen3 8B Instruct` |
| Source model | `Qwen/Qwen3-8B` |
| GGUF repo | `Qwen/Qwen3-8B-GGUF` |
| GGUF filename | `Qwen3-8B-Q4_K_M.gguf` |
| Quantization | `Q4_K_M` |
| License | `Apache-2.0` / `apache-2.0` in machine-readable config |
| Parameters | approximately 8.2B |
| Native context | 32,768 tokens |
| Maximum validated context for profile metadata | 131,072 tokens as upstream extended target; token.place validates only 65,536 in this arc |
| Supported token.place tiers | `8k-fast`, `64k-full` |
| Default mode | non-thinking / thinking disabled |
| 64K extension policy | YaRN/RoPE only when active context exceeds 32,768; initial factor `2.0` over original context `32768` |
| Future 128K policy | out of scope; would require factor `4.0` and separate validation |

### Compatibility alias plan

- During P23b and P23c, `llama-3.1-8b-instruct` remains the canonical default and
  must not alias to Qwen.
- During P23d, preferred rollout behavior is to accept
  `llama-3.1-8b-instruct` as a compatibility alias to the active Qwen profile for
  at least one transition period. This avoids a hard cutover race with DSPACE and
  other OpenAI-compatible clients.
- Alias mapping must be explicit, tested, and applied before scheduler
  filtering. If an old id resolves to the Qwen canonical profile, only Qwen-capable
  nodes should satisfy that request.

## Model-profile architecture

token.place should stop scattering model constants across API catalog,
configuration, desktop fallback metadata, and runtime setup. P23b should add a
single model profile/catalog abstraction, even if the first implementation is a
small typed Python module rather than a persisted database.

A profile should include at least:

- canonical API model id;
- aliases and alias lifecycle metadata;
- display name and docs labels;
- owner, provider, and source model id;
- GGUF repo/id and canonical family URL;
- GGUF filename;
- download URL or Hugging Face repo plus filename;
- expected quantization;
- license;
- parameter class;
- native context tokens;
- maximum validated context tokens;
- token.place supported context tiers;
- context-extension policy, including whether YaRN/RoPE is needed and when;
- chat-template policy;
- non-thinking/thinking policy;
- generation defaults;
- runtime support status, so Qwen metadata can exist before Qwen is advertised as
  runnable;
- docs/display labels for API, desktop, and landing page surfaces.

### Example shape

```python
ModelProfile(
    profile_id="qwen3-8b-q4-k-m",
    api_model_id="qwen3-8b-instruct",
    display_name="Qwen3 8B Instruct",
    owner="Qwen",
    provider="qwen",
    source_model="Qwen/Qwen3-8B",
    parameters="8.2B",
    quantization="Q4_K_M",
    license="apache-2.0",
    gguf_repo="Qwen/Qwen3-8B-GGUF",
    filename="Qwen3-8B-Q4_K_M.gguf",
    download_url="https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
    canonical_family_url="https://huggingface.co/Qwen/Qwen3-8B-GGUF",
    native_context_tokens=32768,
    maximum_validated_context_tokens=131072,
    default_context_tokens=8192,
    supported_context_tiers=("8k-fast", "64k-full"),
    chat_template_policy="gguf_jinja_qwen3_verified",
    thinking_mode="disabled",
    generation_defaults={"temperature": 0.7, "top_p": 0.8},
    rope_scaling_policy={
        "type": "yarn",
        "apply_when_context_exceeds_native": True,
        "original_context_tokens": 32768,
        "targets": {65536: {"factor": 2.0}},
    },
    aliases=(),
    runtime_support="metadata_only_until_p23c",
)
```

The exact names should fit repository conventions. The important requirement is
that API catalog, ModelManager artifact metadata, desktop model bridge fallback,
and relay capability metadata resolve from the same profile data instead of
copying constants.

## API compatibility

### Request validation

- API v1 request validation should canonicalize the requested `model` through the
  profile catalog.
- Aliases are accepted only when intentionally configured and tested.
- Unknown model ids must fail with the existing unsupported-model error shape.
- Alias resolution must be consistent between direct API v1 chat, relay
  selection, compute-node validation, and model catalog lookup.

### `/api/v1/models`

Recommended behavior by phase:

1. **P23b:** keep Llama as the only public advertised model. Qwen may exist as an
   internal profile, but should not be listed as fully available before runtime
   support exists.
2. **P23c:** Qwen can be explicitly runnable in tests/config, but Llama remains
   default. Maintainers may still keep `/api/v1/models` conservative until P23d.
3. **P23d:** list `qwen3-8b-instruct` as the canonical active model. Prefer not
   to list the old Llama id as a separate public model unless maintainers want to
   advertise both. Old ids should be documented as compatibility aliases if they
   remain accepted.

DSPACE should eventually request `qwen3-8b-instruct` directly, but alias support
lets DSPACE migrate after token.place staging is healthy.

## Chat-template handling

Qwen3 chat formatting is a runtime correctness issue, not a cosmetic detail.

- Do not run Qwen through `chat_format='llama-3'`.
- Prefer the GGUF/Jinja chat template when llama-cpp-python exposes it reliably
  for the active Qwen GGUF.
- If current llama-cpp-python exposes a Qwen/Qwen3 `chat_format` or chat handler,
  use it only after verifying that it matches the GGUF template and supports the
  required non-thinking control.
- If no correct Qwen template path is available, fail fast during warm-load or
  model initialization with a clear operator-facing error. Never silently fall
  back to Llama formatting.
- Context admission and generation must use the same render/tokenize template
  path. Exact admission cannot render with one template while generation uses
  another.
- Tests must fail if Qwen initialization includes `chat_format='llama-3'`, if
  Qwen admission uses a Llama formatter fallback, or if template mode cannot be
  identified in safe diagnostics.

Implementation PR P23c must inspect the installed/imported llama-cpp-python API
before choosing exact call shapes. The design should not freeze unverified
constructor or template parameter names.

## Non-thinking mode

API v1 invariant: token.place chat completions are non-reasoning/non-thinking.
Qwen must be configured to produce normal assistant content only.

Required behavior:

- Qwen runs with thinking disabled.
- No `<think>` block may appear in API v1 assistant content.
- Startup/model smoke tests should ask a simple prompt such as `hi` and assert
  that the response contains no `<think>` marker.
- Primary control must be template/runtime configuration, not post-processing.
- Response validation or sanitization may exist only as a defensive fallback.
  Prefer fail-closed rejection such as `compute_node_invalid_model_output` in
  tests if `<think>` appears, because stripping after generation can hide a
  template/configuration bug.
- The implementation must document which mechanism is actually used after
  inspection: `enable_thinking=False`, a Jinja template kwarg, `/no_think`, a
  llama-cpp-python chat handler option, or another runtime-specific equivalent.
- If `/no_think` is needed, it must be injected in the correct template position
  without exposing it to DSPACE or end users as visible content.

No reasoning content, even if later stripped, may be logged or relayed in
plaintext. Compute-side logs may include only safe metadata such as
`thinking_mode=disabled` and `think_output_rejected=true`.

## YaRN/RoPE for 64K

Qwen3-8B's native context is 32,768 tokens. token.place has two context tiers:

- `8k-fast`: 8,192 total context tokens.
- `64k-full`: 65,536 total context tokens.

These are not the same as exact per-request prompt admission. A context tier is a
runtime capacity selected before model load. Exact admission is the compute-side
check that rendered prompt tokens plus output reservation fit inside that active
runtime capacity.

Required Qwen behavior:

- `8k-fast`: no YaRN/RoPE scaling. Applying static YaRN below the native 32K
  context may degrade short-context behavior and must be avoided.
- `64k-full`: apply YaRN/RoPE scaling because 65,536 exceeds Qwen3-8B's native
  32,768 context.
- For 64K, use original/native context `32768`, target context `65536`, and
  factor `2.0`.
- Do not implement 128K in this arc. Future 128K work would use factor `4.0`
  against the native 32,768 context and requires separate design, validation, and
  staging.
- Do not enable YaRN for Llama in this migration.

### ModelManager context handoff

The active desktop/operator context profile must reach ModelManager before the
model is loaded:

1. Operator chooses or config resolves `8k-fast` or `64k-full`.
2. Desktop Rust/Python bridge passes the active context profile into the shared
   compute runtime before warm-load.
3. ModelManager resolves the active model profile and context profile together.
4. ModelManager constructs `llama_cpp.Llama` with `n_ctx` equal to the active
   tier capacity.
5. If the active model profile is Qwen and `n_ctx > native_context_tokens`,
   ModelManager adds the verified YaRN/RoPE kwargs.
6. If the active model profile is Qwen and `64k-full` is requested but the
   installed llama-cpp-python cannot accept/apply the required settings,
   warm-load fails closed and the node must not register as 64K-capable.

P23c must inspect the current llama-cpp-python API for exact constructor fields.
Likely implementation areas include rope scaling type, rope frequency scaling,
YaRN extension factor, and original context fields, but this design intentionally
avoids naming unverified kwargs as absolute truth.

## Desktop app migration

P23d should make the desktop operator experience clearly Qwen-first without
silently deleting existing files.

- Model download/inspect defaults should resolve from the active Qwen profile:
  display name, filename, download URL or HF repo+filename, quantization, license,
  source model, and family URL.
- The resolved model path should point at `Qwen3-8B-Q4_K_M.gguf` by default after
  the switch.
- If an operator already has the old Llama GGUF downloaded, the app must not
  treat that file as satisfying the new Qwen default. The user should be prompted
  to download or select the Qwen GGUF.
- Old Llama files should be left in place. Do not delete large model files
  automatically; users may need them for rollback.
- UI labels should say `Qwen3 8B Instruct` and show `Q4_K_M` where model details
  are displayed.
- Errors should distinguish download failures, missing file, wrong filename,
  unsupported runtime/template, and unsupported YaRN/RoPE for 64K.
- Environment overrides such as `TOKEN_PLACE_DEFAULT_MODEL_FILENAME`,
  `TOKEN_PLACE_DEFAULT_MODEL_URL`, and `TOKEN_PLACE_DEFAULT_MODEL_FAMILY_URL`
  should continue to work unless explicitly deprecated in a later migration.
- macOS and Windows packaging must continue to ship/repair GPU-capable
  llama-cpp-python runtimes for GPU modes. Qwen 64K startup must fail closed if a
  packaging/runtime mismatch prevents required YaRN/RoPE support.

## Landing page migration

P23d should update the relay landing page without changing API v1 semantics.

- The model dropdown/default should prefer the `/api/v1/models` response.
- `EMERGENCY_MODEL_FALLBACK_ID` should become `qwen3-8b-instruct` when Qwen is
  the active default.
- Visible labels and metadata should display Qwen, Q4_K_M, and non-thinking API
  v1 behavior where appropriate.
- Auto, `8k-fast`, and `64k-full` tier behavior remains unchanged.
- Landing chat remains API v1, non-streaming, and relay E2EE-compatible.
- If `/api/v1/models` is unavailable, fallback behavior should not revive Llama
  as the default after P23d.

## Relay and scheduler impact

The relay remains blind to plaintext and model internals. Model migration affects
only safe routing metadata.

- Compute nodes register active model capability with canonical model ids and
  supported context tiers.
- The scheduler filters by resolved requested model id and context tier.
- Alias resolution must be consistent before filtering. For example, after P23d,
  a request for `llama-3.1-8b-instruct` may resolve to canonical
  `qwen3-8b-instruct`; if so, only Qwen-capable nodes should match.
- Mixed Llama/Qwen rollout requires explicit compatibility behavior:
  - Qwen requests must not route to old Llama runtimes.
  - Old Llama-id requests should route to Qwen only if the alias plan says the
    old id now means the current canonical Qwen API v1 model.
  - If maintainers choose a hard cut, old ids must fail clearly rather than
    being opportunistically routed to stale nodes.
- Relay diagnostics may expose active model id, context tier, readiness, and safe
  capability metadata. They must not expose prompts, responses, decrypted token
  counts, tool arguments, private keys, ciphertext payload bodies, or raw
  device-identifying details.

## DSPACE impact

DSPACE follows after token.place staging proves Qwen.

- DSPACE's default token.place API v1 model id should move from
  `llama-3.1-8b-instruct` to `qwen3-8b-instruct` in P23e.
- DSPACE runtime/environment overrides must continue to work so staging can force
  the old id during rollback.
- If token.place P23d preserves `llama-3.1-8b-instruct` as an alias to the active
  Qwen default, DSPACE can migrate after token.place is healthy without a hard
  cutover race.
- DSPACE prompt improvements from P16-P22 should remain provider/model-agnostic.
  Do not rewrite DSPACE prompt planner, RAG, token-lite, save/game schemas, or
  persona behavior as part of this model id migration.

## Testing plan

### P23a documentation checks

- `python -m pytest tests/unit/test_docs_promotion.py -q`
- `python -m pytest tests/unit/test_release_docs_v0_1_1.py -q`
- `pre-commit run --all-files`
- `git diff --check`

### P23b model-profile plumbing tests

- Llama default profile remains unchanged.
- Qwen profile exists with exact metadata: API id, display name, source model,
  GGUF repo, filename, quantization, license, native context, max validated
  context, supported tiers, thinking mode, and YaRN policy.
- ModelManager artifact metadata resolves from the active profile and keeps
  backward-compatible keys.
- Desktop bridge fallback reports Llama defaults before P23d.
- Existing env overrides still win.
- `/api/v1/models` remains backward-compatible and does not advertise Qwen as
  fully usable before runtime support if that conservative policy is chosen.
- API v2 expectations are unchanged.

### P23c Qwen runtime tests

- Mocked llama-cpp inspection tests document supported template and YaRN/RoPE
  kwargs.
- Qwen runtime init omits `chat_format='llama-3'`.
- Qwen runtime init uses the verified GGUF/Jinja or verified Qwen template path.
- Context admission uses the same Qwen render/tokenize path as generation and
  does not use a Llama fallback.
- Non-thinking control is applied via the verified mechanism.
- API v1 output containing `<think>` is rejected or safely handled without
  leaking reasoning content.
- Qwen `8k-fast` does not enable YaRN/RoPE.
- Qwen `64k-full` enables YaRN/RoPE factor `2.0` with original context `32768`.
- Llama 8K/64K behavior remains unchanged.
- Warm-load fails closed for Qwen 64K when YaRN/RoPE support is unavailable.
- Safe diagnostics include model/template/thinking/context/rope metadata and no
  plaintext.

### P23d switch tests

- API v1 model catalog lists Qwen as the primary/default model.
- Old Llama id compatibility alias behavior is explicit and tested.
- Existing invisible aliases such as `gpt-3.5-turbo` and `gpt-5-chat-latest`
  continue mapping to the canonical active API v1 model if that remains current
  policy.
- ModelManager default filename/URL/profile is Qwen.
- Desktop bridge fallback reports Qwen defaults.
- Desktop inspect/download tests ensure a stale Llama file does not satisfy the
  Qwen default.
- Landing page emergency fallback is Qwen.
- Landing page model dropdown/default tests pass.
- Relay model filtering resolves Qwen correctly and does not route Qwen requests
  to stale Llama nodes.
- Diagnostics include safe Qwen metadata.
- API v2 expectations are unchanged.

### Integration, desktop, landing, and manual staging tests

- API model catalog and alias integration tests.
- Model metadata round-trip tests.
- Desktop inspect/download smoke tests on macOS and Windows packaging targets.
- Runtime initialization tests with mocked and, where available, real
  llama-cpp-python.
- Exact context admission tests near 8K and 64K boundaries.
- Landing page Auto/8K/64K tests.
- Manual staging DSPACE prompts after P23e:
  - `hi`
  - `do I have enough green PLA?`
  - `I’d like to make a 3D printed rocket and 10 benchies. Is it enough for that?`
  - `where do I import a gamesave?`
  - a known 64K-ish prompt that stays within the selected tier.
- Manual smoke must record requested tier, selected tier, prompt-token estimate or
  safe local admission result, latency, absence of `<think>`, and response sanity.

## Rollout plan

1. **P23a:** land this design doc only.
2. **P23b:** add model-profile/config/catalog plumbing and Qwen metadata while
   keeping Llama as the default and not advertising Qwen as fully runnable unless
   maintainers intentionally choose otherwise.
3. **P23c:** implement Qwen runtime support: verified chat template,
   non-thinking mode, response validation, and 64K YaRN/RoPE. Llama remains the
   default.
4. **P23d:** switch token.place staging default to Qwen. Update API catalog,
   desktop model surfaces, landing page, docs, tests, relay capability behavior,
   and diagnostics.
5. **Staging soak:** verify 8K and 64K operators, model download/load,
   non-thinking output, YaRN/RoPE diagnostics for 64K, landing chat, old-id alias
   behavior, and DSPACE compatibility.
6. **P23e:** update DSPACE default token.place model id to
   `qwen3-8b-instruct` after token.place staging is healthy.
7. **Production promotion:** promote token.place and DSPACE using existing
   production promotion procedures and the P23f smoke/runbook once added.

### Operational logs and diagnostics to verify

Safe diagnostics should include:

- active profile id;
- canonical API model id;
- display name;
- GGUF filename;
- quantization;
- context tier and `n_ctx`;
- native context tokens;
- chat template mode;
- thinking mode disabled/enforced;
- YaRN/RoPE enabled boolean;
- YaRN/RoPE factor when enabled;
- YaRN original context when enabled;
- backend/offload readiness using existing safe coarse diagnostics.

They must not include plaintext prompts, responses, tool arguments, decrypted
payloads, raw ciphertext bodies, private keys, API keys, or exact token counts
from decrypted user content in relay-owned state.

## Rollback plan

- Keep both Qwen and Llama profiles in the catalog.
- Keep both GGUF files on disk when present; do not auto-delete either file.
- To roll back token.place, switch the default active profile back to Llama by
  env/config/profile selector once P23b/P23d provide that mechanism.
- Restart or re-warm desktop operators after changing the default profile because
  the in-process llama.cpp runtime is constructed with a fixed model file,
  template policy, and context settings.
- If DSPACE has already landed P23e, force its token.place model override back to
  `llama-3.1-8b-instruct` during rollback.
- Verify rollback with `/api/v1/models`, desktop inspect/load, relay diagnostics,
  landing chat, old/new alias behavior, and no `<think>` output.
- If only Qwen 64K fails because YaRN/RoPE is unsupported but Qwen 8K works,
  disable or stop 64K Qwen registration rather than routing 64K requests to an
  unsafe runtime.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Wrong chat template | Poor quality, malformed prompts, hidden reasoning leakage, or invalid token counts | Qwen must not use `chat_format='llama-3'`; verify GGUF/Jinja or Qwen handler; fail fast if unavailable; template-aligned admission/generation tests. |
| Thinking output leakage | Violates API v1 non-thinking invariant and may expose hidden reasoning | Configure thinking disabled at runtime/template level; add smoke prompt; reject or safely handle `<think>` as defensive fallback; never log leaked content. |
| YaRN/RoPE not applied at 64K | 64K tier may fail quality/admission or behave unpredictably beyond native context | Apply Qwen YaRN only when `n_ctx > 32768`; test factor `2.0` and original context `32768`; fail closed if unsupported. |
| YaRN degrading 8K | Short-context quality or latency regression | Do not apply YaRN/RoPE for `8k-fast`; test absence of scaling. |
| GGUF download mismatch | Desktop downloads/loads wrong file or stale Llama file | Centralize filename/repo/url in profile; inspect file path by active profile; do not treat old Llama file as satisfying Qwen; provide clear errors. |
| Mixed node model support during rollout | Requests may route to incompatible nodes | Resolve aliases before scheduling; register canonical capabilities; never route Qwen requests to stale Llama runtimes; test mixed-node filtering. |
| Stale desktop app bundle | Operators may run old UI/runtime against new relay defaults | Diagnostics expose active model metadata; errors explain unsupported model/template; require desktop rebuild/reinstall during P23d staging. |
| DSPACE requesting old model id | DSPACE could fail during staggered deploys | Preserve old id alias during transition or document hard-cut behavior; P23e updates DSPACE; env override remains rollback path. |
| Benchmarks not translating to DSPACE quality | Qwen may pass generic smoke but underperform DSPACE tasks | Run DSPACE smoke prompts after token.place staging; keep prompts provider/model-agnostic; use rollback profile if staging quality fails. |
| llama-cpp-python API drift | Hardcoded kwargs may break across versions | P23c must inspect installed API, encode decisions in comments/tests, and fail clearly when required support is absent. |
| API v2 accidental changes | Incomplete API v2 could receive runtime traffic | Scope-lock tests/reviews: no API v2 route/runtime changes in P23a-P23d. |

## Acceptance criteria for P23a

- This design is detailed enough to drive P23b-P23e implementation prompts.
- It includes the complete migration map, test plan, rollout plan, and rollback
  plan.
- It explicitly preserves API v1 non-thinking behavior and 64K context via
  YaRN/RoPE.
- It does not change runtime behavior.
