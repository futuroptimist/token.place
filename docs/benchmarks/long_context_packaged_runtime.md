# Long-context packaged-runtime benchmark harness

The long-context harness turns the long-context macOS Metal evidence from #1566 into a repeatable,
privacy-safe benchmark surface for downstream comparisons. Ordinary CI runs only deterministic unit and
contract tests; it does **not** download a model, require a GPU, launch a packaged desktop app, or
run multi-minute inference.

## Prerequisites for physical runs

Physical packaged-runtime mode is intentionally fail-closed. Run it only on a machine with:

- a built or installed token.place desktop application at version `0.1.16`;
- the existing pinned `llama-cpp-python==0.3.32` packaged runtime;
- Qwen3 8B Q4_K_M or another explicitly recorded local model artifact;
- macOS Apple Silicon with Metal or Windows with NVIDIA/CUDA for GPU validation, with CPU only where
  the desktop runtime already supports it;
- API v1 E2EE relay request, encrypted progress, cancellation, and response paths configured.

The harness never substitutes a fake runtime or trusts an opaque HTTP adapter. It invokes the
repository-owned desktop WebDriver runner with explicit arguments and bounded execution. The runner
launches the selected installed/package-built application, configures its operator, and sends the
fixture with the existing landing-page API v1 E2EE browser flow:

```bash
python scripts/long_context_benchmark.py packaged-runtime \
  --app-binary "$BENCHMARK_APP_BINARY" \
  --model "$BENCHMARK_MODEL" \
  --backend metal \
  --relay-url "$BENCHMARK_RELAY_URL" \
  --fixture small-8k \
  --scenario structured-extraction \
  --context-tier 8k-fast \
  --request-timeout 600 \
  --cleanup-timeout 30 \
  --trials 3 \
  --out-dir .tmp/long-context
```

`--trials` defaults to `1` and accepts `1` through `10`. Trials run sequentially with the same
validated fixture, manifest, app, model, backend, tier, relay, and timeout configuration. Each
invocation retains its own protected temporary files, WebDriver/app resources, timeout budget, and
bounded cleanup; trials are never concurrent. The command stops at the first runtime-contract
failure and never labels a partial run as a completed aggregate.

The `8k-fast` context window is 8,192 tokens and retains its 1,024-token output reservation, leaving
an effective prompt budget of 7,168 tokens. The `small-8k` fixture therefore requests 7,168 prompt
tokens, and its manifest reports that request separately from the bounded actual CI estimate. Both
small-fixture scenarios remain close to the request without exceeding the effective prompt budget.
The harness validates this accounting before creating temporary files or launching the runner; it
never truncates a prompt, lowers the output reservation, or silently switches tiers.

On Windows/NVIDIA use the same command with `--backend cuda` and the installed `.exe`. On macOS use
the installed `.app` executable under `Contents/MacOS` and `--backend metal`. For existing packaged
CPU support use `--backend cpu`; the runner selects operator CPU mode and requires
`Backend selected=cpu` and `Backend used=cpu`. The runner attests that
the launcher source is bundled, active and bundled runtime IDs agree, and selected/used backend
matches the requested backend. It fails closed for mock/dev substitution, absent progress, an
incomplete response lifecycle, or cleanup failure.

Before launch, inputs are validated fail closed: the model must be a
readable regular file, the app must be an executable regular file, and request/cleanup timeouts must
be finite and positive. External E2EE relays require HTTPS; loopback relays may use HTTP or HTTPS.
Credentials, fragments, malformed ports, and other schemes are rejected. Temporary request,
evidence, and diagnostic files are owner-only on POSIX, portable to Windows Python 3.11, and closed
and deleted after each run. Phase checkpoints use owner-only, unique temporary files and atomic
replacement; Windows sharing denials during publication are retried only until a small deadline, so
polling never exposes partial JSON. Windows `PermissionError` is also treated as potential
contention only while replacing the checkpoint's unique owner-created temporary file, or removing
that same temporary artifact; unrelated filesystem operations retain their existing strict error
handling. Owner-created logs and directories are closed and removed with
the same bounded, Windows-lock-tolerant cleanup policy after the exact owned process tree has been
asked to exit. A cleanup failure is reported categorically and separately without replacing an
earlier runner failure. Runner output is written to a temporary bounded diagnostic tail rather than
buffered without limit. On timeout the harness targets only the process tree it created after the
runner's bounded cleanup opportunity; it never matches broad process names. Unit tests replace only
the subprocess boundary and are orchestration evidence, never physical Metal/CUDA evidence.

This #1566 physical-validation follow-up to #1631 and #1634 does not claim Windows/CUDA success from
unit tests. After merge, rerun the same one-cell Windows/CUDA `small-8k` / `single-needle` /
`64k-full` physical gate against unchanged desktop `0.1.16` and `llama-cpp-python==0.3.32` before
attempting `8k-fast` classification or the six-cell matrix. Physical macOS/Metal validation also
remains outstanding.

## Fixture generation

Generate deterministic, synthetic, privacy-safe fixtures instead of committing large prompt blobs:

```bash
python scripts/long_context_benchmark.py generate-fixture --fixture small-8k --scenario single-needle --out-dir .tmp/long-context-needle-small
python scripts/long_context_benchmark.py generate-fixture --fixture intermediate-32k --scenario single-needle --out-dir .tmp/long-context-needle-middle
python scripts/long_context_benchmark.py generate-fixture --fixture long-55k --scenario single-needle --out-dir .tmp/long-context-needle-late
python scripts/long_context_benchmark.py generate-fixture --fixture long-55k --scenario structured-extraction --out-dir .tmp/long-context-structured
```

The fixture manifest records the fixture version, deterministic seed, requested token count, actual
estimated CI-tokenizer count, prompt SHA-256, expected answers, scoring rules, and requested and
actual token offsets/ratios plus UTF-8-safe target-prefix cut points for every target. The cut
points identify the start of answer-bearing prose or record values, never a table-of-contents or
chapter-heading decoy. Structured cuts follow the complete `Chapter <key>: <heading>` line;
needle and canary cuts follow `NEEDLE FACT: ` and `RECORD CANARY: ` respectively. They are bound to
the prompt SHA-256 and validated before a packaged launch. Generator callbacks and the deterministic
`whitespace-ci` counter are always labeled non-authoritative estimates. A packaged report records
that estimate separately from the runtime admission/progress count, and uses only the latter for
physical throughput. Missing or inconsistent authoritative totals or target-offset evidence fails
the packaged contract closed.

The current synthetic fixture IDs are:

| Fixture | Requested size | Purpose |
| --- | ---: | --- |
| `small-8k` | 7,168 prompt tokens in an 8,192-token window | `8k-fast` validation with a 1,024-token output reservation |
| `intermediate-32k` | 32,768 tokens | mid-depth haystack validation |
| `long-55k` | 55,254 tokens | approximate `64k-full` benchmark comparable to #1566 |

The `single-needle` scenario plants one simple needle near the early, middle, or late depth for the
8K, 32K, or 55K tier respectively. Its exact one-key JSON oracle scores the needle value; the exact
needle occurs once and deterministic similar-but-distinct markers are decoys. The separate
`structured-extraction` scenario asks only for VII/XIV/XXI/canary, retaining table-of-contents and
heading/prose ambiguity. Its canary literal is not disclosed by the instructions and occurs once.

In explicit long-context benchmark mode, the packaged runner passes only the fixture hash, validated UTF-8 prefix cut
points, and an owner-only evidence location to the bundled sidecar. During normal authoritative
admission, the sidecar verifies the actual final user message against that hash and sends the full
message and every prefix through the same loaded `llm_instance`, `render_and_tokenize_chat` bridge,
chat-template policy, and thinking option. It atomically writes bounded counts and runtime identity;
it never writes prompt text, target values, ciphertext, credentials, or request identifiers. The
seam is inert unless the manual long-context benchmark runner explicitly supplies both environment variables.

The harness requires method `packaged_admission_render_and_tokenize_chat`, matching bundled runtime
identity and total/progress counts, the exact target key set, unique ordered positive prefix counts,
and ratios within an absolute 0.03 tolerance of the controlled fixture placements. Missing,
malformed, stale-hash, identity/total mismatch, ambiguous ordering, or out-of-tolerance evidence
fails closed. Fixture whitespace/callback estimates are never relabeled as physical evidence. No
Metal, CUDA, or CPU tokenizer run is claimed by this documentation.

To run a separately stored golden prompt, supply its manifest as a required pair (the harness does
not infer an oracle from model output):

```bash
python scripts/long_context_benchmark.py packaged-runtime \
  --app-binary "$BENCHMARK_APP_BINARY" --model "$BENCHMARK_MODEL" --backend metal \
  --relay-url "$BENCHMARK_RELAY_URL" --context-tier 8k-fast \
  --scenario structured-extraction \
  --prompt /path/to/small-8k.prompt.txt \
  --manifest /path/to/small-8k.manifest.json \
  --out-dir .tmp/long-context-external
```

The pair is rejected before runner launch if either file is absent, the prompt exceeds the bounded
size, the SHA-256 differs, or fixture identity, scenario, seed, oracle, scoring, token provenance,
or target metadata is missing, malformed, out of bounds, or unordered.

## Semantic evaluation

Evaluate a response against a manifest:

```bash
python scripts/long_context_benchmark.py evaluate \
  --manifest .tmp/long-context-fixtures/small-8k.manifest.json \
  --response .tmp/long-context-fixtures/response.json \
  --strict \
  --out-dir .tmp/long-context-report
```

Strict mode exits nonzero unless the complete exact-match result passes. Report-only baselines can
record semantic failures without relabeling them as passes by omitting `--strict`.

Semantic sub-scores are reported separately for:

- JSON-only response with no Markdown/commentary;
- exact key set;
- exact canary retrieval;
- target/chapter selection;
- prose-versus-heading selection;
- exact whitespace-separated word count;
- capitalization preservation;
- trailing-punctuation rules;
- complete exact match.

The known estimator-validation failure shape is intentionally rejected: `VII` with six words (`They were obliged to
camp out`) fails word count and exact match, while `XIV`/`XXI` chapter-title substitutions fail
prose and target selection even though JSON shape and canary can still pass.

## Metrics and report schema

Reports use schema `long-context-benchmark-report-v2` and are written atomically as
`long_context_benchmark_report.json` in the selected output directory. Reports are sanitized for GitHub issue
attachment: prompt/response bodies, ciphertext, IVs, keys, cancellation tokens, high-cardinality
request/client/session IDs, absolute user paths, secrets, and unbounded subprocess output are
removed or redacted. The sanitized document is validated before atomic replacement. A missing key,
wrong type or enum, non-finite number, inconsistent mode-specific field, or unsupported schema or
fixture version fails the command; an existing destination report remains untouched.

Every report has `schema_version`, `mode`, categorical `status`, and a fixture identity containing
`id`, `version`, `scenario`, and SHA-256. Semantic reports additionally require the complete semantic
score. A successful packaged-runtime contract requires:

- packaged app, build, bundled runtime, and safe model-fingerprint identity;
- requested, selected, and used backend (`cpu`, `metal`, or `cuda`);
- context tier, context window, output reservation, authoritative prompt count, and output count;
- separately validated authoritative packaged-local progress and best-effort encrypted delivery;
- every timing, throughput, request-budget, and completion-margin field;
- the complete semantic score and aggregate trial count, exact-match count, and pass rate.
- requested/completed trial counts, per-category failure counts, and bounded per-trial boolean/error
  summaries (never response text).
- the generation settings observed from the plaintext API v1 request immediately before browser
  encryption.

Generation evidence is an allowlisted object. `supplied` records only bounded scalar request
options (`max_tokens`, and `temperature`, `top_p`, or `seed` when actually present).
`omitted_runtime_default` names relevant options absent from the request; the harness does not
invent or infer their runtime defaults. The current landing-page request supplies `max_tokens` and
omits `temperature`, `top_p`, and `seed`. Missing, malformed, non-finite, out-of-range, unsupported,
or cross-trial-inconsistent evidence fails the runtime contract. The capture never retains
messages, prompt/response content, request identifiers, ciphertext, credentials, or keys.

The aggregate is computed only after every requested trial completes its runtime contract. Overall
success requires every trial to be semantically exact. `--report-only` can return zero for mixed
semantic outcomes only after all requested runtime trials completed; exact-match count, pass rate,
failure-category counts, and `overall_pass=false` remain truthful in the report. Each trial receives
the full `--request-timeout`; cleanup remains bounded separately by `--cleanup-timeout`.

### Packaged-runner phase budgets and watchdog

The two existing CLI flags remain sufficient; no additional argument is required. The packaged
runner applies five separate allowances:

- **setup/readiness: 300 seconds** for `tauri-driver`, WebDriver, desktop UI, operator provisioning,
  CUDA/Metal model warm-load, relay registration, and landing-page readiness. Landing readiness
  checks Vue, the real client keypair, model-catalog selection, and the requested context tier; it
  deliberately excludes message-dependent Send eligibility. The runner enters the prompt through
  the ordinary message input before checking final Send eligibility;
- **inference request: `--request-timeout` seconds**, beginning immediately before the send-button
  click that submits the request, so setup cannot consume inference time;
- **evidence finalization: 120 seconds per window**: one window snapshots generation settings,
  post-terminal observations, tokenizer/KV evidence, and primary-trial memory before cancellation;
  when cancellation validation is enabled, a fresh 120-second window after cancellation covers
  model fingerprinting and the atomic evidence write; and
- **cancellation validation: zero when disabled, otherwise
  `2 × request timeout + 2 × observation window + 8 × recovery timeout` seconds** for the two
  progress-trigger waits, two quiescence windows, two asynchronous acknowledgements, two scenario
  follow-ups, operator stop, restart stability, relay registration, and the post-restart follow-up; and
- **cleanup: `--cleanup-timeout` seconds**, reserved for the exact process tree owned by the trial.

The parent watchdog is finite and uses the explicit equation
`300 + request timeout + 120` seconds when cancellation validation is disabled, or
`300 + request timeout + 120 + cancellation validation + 120` seconds when it is enabled,
for child execution, followed by at most the configured cleanup budget. Thus the complete overall
allowance is `setup + request + finalization + cleanup` without cancellation, or `setup + request +
pre-cancellation finalization + cancellation validation + post-cancellation finalization + cleanup`
with it; neither equation contains an undocumented multiplier or an unbounded wait. Cancellation
validation retains its existing bounded waits and CLI controls; the named additive budget does not
change cancellation semantics or add a required argument. Pre-request WebDriver and readiness waits
draw only from the shared setup deadline, not from the request timeout. Primary-evidence finalization
begins immediately after the primary response. Cancellation then receives its independent complete
allowance, followed by a fresh complete finalization window. Without cancellation, the initial
finalization window covers the remaining work.

An owner-only phase file is atomically replaced at allowlisted boundaries (`runner_startup`,
`webdriver_ready`, `desktop_ready`, `operator_ready`, `landing_page_ready`, `request_active`,
`response_received`, `cancellation_validation`, `evidence_finalization`, and `cleanup`). Cancellation
uses one finite deadline, while primary evidence is captured within the pre-cancellation
finalization deadline. A fresh evidence-finalization allowance starts after cancellation finishes;
when cancellation is disabled, only the initial finalization deadline applies. A child that reaches
`cleanup` within the work deadline may use the one reserved cleanup window; child cleanup and
parent-enforced exact-tree teardown share that window and cannot add a second allowance. If the
parent watchdog expires, the
`packaged_runner_timeout` report records only the last safe phase, the five configured budgets and
their derived runner/overall totals, bounded elapsed time, and whether owned-tree cleanup succeeded.
The channel contains no prompts, responses, ciphertext, keys, credentials, identifiers, paths,
command lines, or logs. A nonzero child exit retains only its last safe phase, one allowlisted
categorical failure reason, bounded elapsed time, and the owned cleanup outcome when available; it
never retains exception strings, traceback text, or a raw diagnostic tail. Missing, malformed, or
stale phase state fails closed as a runtime-contract failure, and all request, response, diagnostic,
and phase files are deleted after the attempt.

The sanitized Windows 11/NVIDIA attempt described for this follow-up completed zero trials: its
child runner failed at the impossible pre-prompt Send-eligibility wait. It therefore supplied **no
semantic baseline**. Corrected readiness ordering and mocked orchestration tests are not physical
CUDA or Metal validation; physical Windows/CUDA and macOS/Metal reruns remain required.

Failed or `not_run` packaged reports require a stable categorical failure code. Report validation
does not fill absent telemetry with zero and does not allow `NaN` or infinity.

Physical runs report these low-cardinality fields:

- runtime/app version and build ID;
- benchmark and fixture versions;
- safe model identifier and artifact fingerprint, not absolute path;
- requested/available/selected/actual backend;
- context tier and context window;
- requested/actual prompt tokens, output reservation, and actual output tokens;
- batch/runtime profile, `n_batch`, `n_ubatch`, K/V types, Flash Attention, KQV offload, offloaded
  layers, fallback/recovery diagnostics, and YaRN/RoPE configuration;
- same-origin worker preparing, prefill, and first-token durations; independent parent inference
  duration; runner end-to-end duration; prompt throughput; request budget; and remaining margin;
- authoritative local phase counts, encrypted phases actually delivered, total consistency,
  processed-never-exceeds-total, cancellation timing, and worker recovery timing.

Validate JSON syntax before attachment:

```bash
python -m json.tool .tmp/long-context-report/long_context_benchmark_report.json >/dev/null
```

## Progress, cancellation, and recovery invariants

The authoritative runtime stream is the packaged operator's privacy-safe
`api_v1.local_progress` records after the driver-log byte boundary captured immediately before the
primary request. The runner takes that bounded snapshot as soon as the atomic response arrives,
before any cancellation or recovery request. It parses only the exact old-app record shapes and
the matching `api_v1.inference_complete` record; arbitrary surrounding log text and correlation
identifiers are discarded. Multiple request/worker correlations, malformed records, or an
ambiguous completion fail closed.

Local progress permits only `preparing`, `prefill`, and `generating`. Sequence and elapsed values
are monotonic, phases cannot regress or skip, counters are non-negative, cached is at most processed,
and processed is at most total. An initial pre-authoritative `preparing` event may truthfully carry
total zero. The first positive total becomes stable. Completion requires full prompt processing,
a genuine local generating event, positive generated-token progress, and agreement between local,
admission/tokenizer, and final response prompt counts.

Browser-observed encrypted P6 progress is validated separately as best-effort delivery. Every
delivered event must be an exact, monotonic, schema-valid projection of the authoritative local
stream, but terminal completion may overtake a coalesced generating update. Reports list only the
phases actually delivered and set `terminal_overtook_generating_update` when appropriate; they
never synthesize a browser phase. Atomic response completion and a short monotonic-deadline-bounded
post-terminal silence check remain independent requirements. This preserves P6's one-latest-pending
coalescing and terminal-discard behavior: encrypted progress never delays or changes the response.

Runtime preparation, prefill, and time-to-first-token are derived only between worker-progress
`elapsed_ms` boundaries. Parent inference duration and the browser runner's end-to-end monotonic
duration are separate provenance fields: they have no proven common origin, so the report never
subtracts one from another to fabricate decode duration or decode throughput. Request-budget
compliance and completion margin use only the runner end-to-end duration. Prompt tokens come from
admission/tokenizer evidence and validated local progress,
while completed output tokens come exclusively from allowlisted final response
`usage.completion_tokens` (with `finish_reason` retained). The last coalesced progress counter and
response-text estimates are not output-token authority. Missing, non-finite, reversed, inconsistent,
or over-budget evidence fails closed rather than being coerced or inferred.

Cancellation scenarios must be progress-triggered, not sleep-only:

```bash
python scripts/long_context_benchmark.py packaged-runtime \
  --model /path/to/local-model.gguf \
  --backend metal \
  --relay-url http://127.0.0.1:8000 \
  --request-timeout 600 \
  --cleanup-timeout 30 \
  --cancellation-validation \
  --prefill-cancel-fraction 0.5 \
  --generation-cancel-tokens 8 \
  --cancellation-observation-window 0.5 \
  --cancellation-recovery-timeout 30 \
  --out-dir .tmp/long-context-prefill-cancel \
  --report-only
```

`--cancellation-validation` is opt-in and executes exactly one validation sequence per packaged CLI
invocation, independently of `--trials`. Specify exactly one prefill trigger: a positive
`--prefill-cancel-tokens` count, or a strict fraction between zero and one with
`--prefill-cancel-fraction`. `--generation-cancel-tokens` is a positive bounded generated-token
threshold. The runner waits for an observed, nonterminal progress event in the requested phase; it
does not use elapsed sleep as the trigger and fails if the phase or threshold is missed. A
mid-prefill trigger additionally requires authoritative, positive `total_prompt_tokens` evidence
and the strict relationship `0 < threshold <= trigger_count < total_prompt_tokens`. A telemetry
jump directly to completed prefill fails closed rather than being labeled mid-prefill, and both
cancellation scenarios must report the same total as the packaged request.

For both prefill and generation, the installed landing page's existing `cancelRelayRequest()` and
`terminateRelayRequestLocally()` paths perform the cancellation. The evidence requires a real
attempt, relay acknowledgement, cleanup within `--cleanup-timeout`, and a bounded quiescence window
with no later progress, successful result, or active generation. A small ordinary encrypted
follow-up request must then complete on the recovered worker. After both scenarios, the runner
clicks **Stop operator** and **Start operator**, requires bounded readiness and a changed operator
session, and confirms the restarted worker with another small encrypted request.

The `cancellation_recovery` report section contains only phases, configured/observed counters,
booleans, bounded durations, stale/late-event counts, and `session_changed`. It never contains a
prompt, response, request/session identifier, cancellation token, credential, key, ciphertext, or
relay payload. Interrupted cancellation requests are not semantic trials. Missed thresholds,
unconfirmed cancellation, late results, stale progress, cleanup/recovery timeout, failed follow-up,
unchanged worker session, restart failure, and malformed evidence are runtime-contract failures;
`--report-only` cannot suppress them.

The command above is the genuine-hardware procedure. It was not executed during development of
this harness change; a packaged 0.1.16 application, model, relay, and supported hardware are
required before recording physical evidence.

## Physical process-tree memory

Physical packaged runs require an RSS summary sampled with `psutil` from the process tree rooted at
the `tauri-driver` process created by that trial. Each sample sums the root's RSS with the RSS of its
currently visible packaged-app and sidecar descendants; unrelated browser and system processes are
not selected by name or included. Sampling occurs before the primary request, during its bounded
polling loop, and after terminal observation. Processes that disappear or become inaccessible while
the tree is enumerated are skipped, but a run fails closed if it obtains no valid owned-tree sample.

Reports retain only the method/scope/platform enums, sample count, and baseline/peak/final RSS byte
counts for each sequential trial, plus the maximum peak RSS across trials. They contain no PIDs,
process names, executable paths, command lines, usernames, probe output, or request content. RSS is
noisy OS-accounted resident memory: it is not GPU VRAM, Metal unified-memory residency, or evidence
that P7's estimated KV allocation matches the runtime GGML diagnostic.

## Runtime-configuration attestation

Every physical trial contains one exact-shape `runtime_configuration` attestation read from the
current packaged worker's labeled status and sanitized `Readiness diagnostics` surface. It records:

- requested/effective compute mode and requested/available/selected/used backend, plus the bounded
  fallback reason;
- context tier and effective context window;
- selected, preferred, and attempted runtime profiles, recovery count, construction result, and
  profile fallback reason;
- requested/selected batch profile, `n_batch`, and `n_ubatch`;
- KV precision, `type_k`, `type_v`, and KV-cache device;
- Flash Attention, KQV offload, and the bounded offloaded-layer value; and
- requested/original context tokens, context multiplier, RoPE frequency scale, extension-factor
  override status, RoPE scaling-source classification, and YaRN configuration-valid status.

Profile-dependent fields for a positively verified non-Qwen/non-64K runtime use only
`{"status":"not_applicable","reason":"not_qwen_64k_profile"}`; the harness never invents
profile or YaRN values. Qwen3 `64k-full` requires the complete valid YaRN/RoPE object (65,536
requested, 32,768 original, multiplier 2.0, frequency scale 0.5, no extension-factor override,
and an applicable scaling source). The attestation is cross-checked against the report backend and
context and against P7 profile/KV evidence. Unknown keys, arbitrary strings, missing fields,
malformed types, contradictory values, and cross-trial drift fail the runtime contract even with
`--report-only`. Reports retain `runtime_configuration.trials`, one consistent entry per completed
physical trial, and never retain arbitrary readiness keys, paths, identifiers, logs, or payloads.

## Deterministic benchmark matrix

Emit the machine-readable execution plan without launching a desktop runtime:

```bash
python scripts/long_context_benchmark.py matrix-plan > matrix.json
python -m json.tool matrix.json >/dev/null
```

The `long-context-benchmark-matrix-plan-v1` plan is deterministic and duplicate-free. For each
declared packaged platform/backend (macOS Metal, Windows/NVIDIA CUDA, and the existing packaged CPU
mode on Linux, macOS, and Windows), it contains three sequential trials for both `single-needle`
and `structured-extraction` in each required context cell: `8k-fast`/`small-8k`,
`64k-full`/`intermediate-32k`, and `64k-full`/`long-55k`. It also contains exactly one separate
progress-triggered cancellation/recovery sequence for the `64k-full`/`long-55k`
`structured-extraction` cell. A matrix plan, schema test, or injected unit fake is scheduling and
orchestration evidence only; none is physical benchmark evidence. This documentation does not claim
that any Metal, CUDA, or CPU matrix cell was executed.

## Exit codes

- `0`: requested CI-safe operation passed, or packaged-runtime produced passing runtime evidence.
- `1`: strict semantic, packaged-runtime invariant, cancellation, recovery, telemetry, privacy, or
  adapter contract failure. `--report-only` may preserve semantic-failure reports but does not
  suppress runtime, telemetry, cancellation, recovery, privacy, or invariant failures.
- `2`: invalid CLI input or missing required packaged-runtime arguments.

For packaged mode, `--report-only` records semantic failure without changing `semantic_pass` or the
overall benchmark `pass` to true. It exits zero only when the separate runtime-contract result
(identity, backend, progress, cleanup, timing, and telemetry) passed and semantic exactness was the
only failure. Strict/default mode and every runtime-contract failure exit nonzero.

## CI versus hardware validation

Ordinary CI should run:

```bash
python -m pytest -q tests/unit/test_long_context_benchmark_harness.py
python -m pytest -q tests/unit
pre-commit run --all-files
git diff --check
./run_all_tests.sh PR
```

Physical Metal/CUDA validation is manual and should attach only sanitized reports to #1566, #1608,
or downstream validation. Do not claim 0.1.16 release validation or general semantic correctness
from a report-only baseline.

### Qwen 64K KV allocation diagnostics

Applicability comes from the active runtime's verified architecture and selected profile, never the
GGUF filename. A supported Qwen model using `64k-full` therefore requires two independent,
same-session records. The selected runtime profile supplies the exact GGUF-header-derived KV
allocation, backend, context size, K/V types, profile ID, metadata source, and fallback state. The
pinned `llama-cpp-python` 0.3.32 runtime (llama.cpp commit
`b3fed31b99f9bd37725833674252bccb429bb183`) separately supplies its initialization-time
`KV buffer size` diagnostics. Multiple backend buffers are summed only when their device labels
are unique within that initialization attempt.

The runtime diagnostic prints MiB to one or two decimal places, so it is not byte-exact. For each
record the half-unit precision is `ceil(1 MiB / (2 * 10**decimal_places))`; the total
`precision_bytes` must equal that value times `record_count`. Comparison uses the resulting interval.
Missing, stale, duplicate-device, mixed-precision,
overflowed, fallback-derived, backend/context-mismatched, or out-of-interval evidence fails the
runtime contract and cannot be suppressed by `--report-only`. Reports retain one bounded comparison
per sequential trial with exact validated keys for profile, backend, context, K/V types, estimated
and observed bytes, delta, precision interval inputs, provenance enums, applicability, and pass state.
Only a positively attested non-Qwen architecture or non-64K Qwen profile is not applicable; missing
or malformed applicability evidence fails closed.

Only bounded numeric/categorical summaries are retained. Raw stderr, paths, PIDs, command lines,
prompts, responses, credentials, and runtime/session identifiers are excluded. The pinned fixture
is source-derived parser/accounting evidence, not proof of a physical Metal or CUDA run. No physical
Metal or CUDA KV validation was performed for this change.
