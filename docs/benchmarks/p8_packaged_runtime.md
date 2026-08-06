# P8 packaged-runtime benchmark harness

The P8 harness turns the long-context macOS Metal evidence from #1566 into a repeatable,
privacy-safe benchmark surface for P9 comparisons. Ordinary CI runs only deterministic unit and
contract tests; it does **not** download a model, require a GPU, launch a packaged desktop app, or
run multi-minute inference.

## Prerequisites for physical runs

Physical packaged-runtime mode is intentionally fail-closed. Run it only on a machine with:

- a built or installed token.place desktop application at version `0.1.12`;
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
python scripts/p8_benchmark.py packaged-runtime \
  --app-binary "$P8_APP_BINARY" \
  --model "$P8_MODEL" \
  --backend metal \
  --relay-url "$P8_RELAY_URL" \
  --fixture small-8k \
  --scenario structured-extraction \
  --context-tier 64k-full \
  --request-timeout 600 \
  --cleanup-timeout 30 \
  --out-dir .tmp/p8
```

The fixture's authoritative token count plus the selected tier's 1,024-token output reservation
must fit before any temporary file or runner process is created. In particular, `small-8k` is
slightly larger than 8,192 tokens by construction and therefore requires the explicit
`--context-tier 64k-full` shown above; the harness never truncates it or silently switches tiers.

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
and deleted after each run. Runner output is written to a temporary bounded diagnostic tail rather
than buffered without limit. On timeout the harness targets only the process tree it created after
the runner's bounded cleanup opportunity; it never matches broad process names. Unit tests replace only the subprocess
boundary and are orchestration evidence, never physical Metal/CUDA evidence.

## Fixture generation

Generate deterministic, synthetic, privacy-safe fixtures instead of committing large prompt blobs:

```bash
python scripts/p8_benchmark.py generate-fixture --fixture small-8k --scenario single-needle --out-dir .tmp/p8-needle-small
python scripts/p8_benchmark.py generate-fixture --fixture intermediate-32k --scenario single-needle --out-dir .tmp/p8-needle-middle
python scripts/p8_benchmark.py generate-fixture --fixture long-55k --scenario single-needle --out-dir .tmp/p8-needle-late
python scripts/p8_benchmark.py generate-fixture --fixture long-55k --scenario structured-extraction --out-dir .tmp/p8-structured
```

The fixture manifest records the fixture version, deterministic seed, requested token count, actual
estimated CI-tokenizer count, prompt SHA-256, expected answers, scoring rules, and requested and
actual token offsets/ratios for every target. Generator callbacks and the deterministic
`whitespace-ci` counter are always labeled non-authoritative estimates. A packaged report records
that estimate separately from the runtime admission/progress count, and uses only the latter for
physical throughput. Missing or inconsistent authoritative totals or target-offset evidence fails
the packaged contract closed.

The current synthetic fixture IDs are:

| Fixture | Requested size | Purpose |
| --- | ---: | --- |
| `small-8k` | 8,192 tokens | fast unit/contract validation |
| `intermediate-32k` | 32,768 tokens | mid-depth haystack validation |
| `long-55k` | 55,254 tokens | approximate `64k-full` benchmark comparable to #1566 |

The `single-needle` scenario plants one simple needle near the early, middle, or late depth for the
8K, 32K, or 55K tier respectively. Its exact one-key JSON oracle scores the needle value; the exact
needle occurs once and deterministic similar-but-distinct markers are decoys. The separate
`structured-extraction` scenario asks only for VII/XIV/XXI/canary, retaining table-of-contents and
heading/prose ambiguity. Its canary literal is not disclosed by the instructions and occurs once.

The packaged runner currently reports the admission total through encrypted progress, but the
installed desktop exposes no reusable control seam for applying that identical admission tokenizer
to each target prefix. Consequently physical target-depth validation fails closed with
`authoritative_target_depth_unavailable` and names the missing
`packaged_admission_render_and_tokenize_chat_prefix_counts` seam. Fixture whitespace/callback
estimates are never relabeled as physical evidence. A successful evidence envelope must eventually
record the packaged tokenizer method and runtime identity plus independently measured total and
target-prefix counts; documentation does not claim that evidence has been produced yet.

To run a separately stored golden prompt, supply its manifest as a required pair (the harness does
not infer an oracle from model output):

```bash
python scripts/p8_benchmark.py packaged-runtime \
  --app-binary "$P8_APP_BINARY" --model "$P8_MODEL" --backend metal \
  --relay-url "$P8_RELAY_URL" --context-tier 64k-full \
  --scenario structured-extraction \
  --prompt /path/to/small-8k.prompt.txt \
  --manifest /path/to/small-8k.manifest.json \
  --out-dir .tmp/p8-external
```

The pair is rejected before runner launch if either file is absent, the prompt exceeds the bounded
size, the SHA-256 differs, or fixture identity, scenario, seed, oracle, scoring, token provenance,
or target metadata is missing, malformed, out of bounds, or unordered.

## Semantic evaluation

Evaluate a response against a manifest:

```bash
python scripts/p8_benchmark.py evaluate \
  --manifest .tmp/p8-fixtures/small-8k.manifest.json \
  --response .tmp/p8-fixtures/response.json \
  --strict \
  --out-dir .tmp/p8-report
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

The known P7 failure shape is intentionally rejected: `VII` with six words (`They were obliged to
camp out`) fails word count and exact match, while `XIV`/`XXI` chapter-title substitutions fail
prose and target selection even though JSON shape and canary can still pass.

## Metrics and report schema

Reports use schema `p8-benchmark-report-v1` and are written atomically as
`p8_benchmark_report.json` in the selected output directory. Reports are sanitized for GitHub issue
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
- the validated progress summary and ordered terminal/result evidence;
- every timing, throughput, request-budget, and completion-margin field;
- the complete semantic score and aggregate trial count, exact-match count, and pass rate.

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
- preparing, prefill, first-token, decode, total duration, throughput, request budget, and remaining
  margin;
- progress counts, first/final progress, monotonicity, total consistency, processed-never-exceeds
  total, cancellation timing, and worker recovery timing.

Validate JSON syntax before attachment:

```bash
python -m json.tool .tmp/p8-report/p8_benchmark_report.json >/dev/null
```

## Progress, cancellation, and recovery invariants

The evidence stream is ordered. Each observation has a strictly increasing integer `sequence` and
strictly increasing non-negative integer `elapsed_ms` in the request's monotonic clock domain.
Progress observations use only the production phases `preparing`, `prefill`, and `generating`;
phases may repeat or advance one step and never regress or skip a phase after observation begins.
Every progress observation contains a positive, stable `total_prompt_tokens` plus non-negative
`cached_prompt_tokens`, `processed_prompt_tokens`, and `generated_tokens`. Processed and generated
counts are monotonic, cached never exceeds processed, and processed never exceeds total.

The successful API response is an ordered `result` observation followed by exactly one ordered
`terminal` observation with state `completed`. Cancellation and failure instead use `cancelled` or
`failed`. Completion requires at least one progress observation, a generating phase, exactly one
successful result, and full prompt processing. Duplicate or conflicting terminal observations,
terminal timestamps preceding prior progress, results after cancellation, and any result or
progress after terminal are categorical failures. The desktop runner continues polling for a short
monotonic-deadline-bounded window after completion so late progress or a conflicting result is
observable; it does not use a multi-second fixed sleep. Missing or malformed lifecycle telemetry
returns stable errors such as `progress_missing`, `malformed_telemetry`, `incomplete_prefill`, or
`progress_after_terminal` rather than passing or raising an uncaught exception.

Timings use five ordered monotonic boundaries: request start, end of preparing, end of prefill,
first generated token, and request end. They produce preparing duration, prefill duration,
time-to-first-token, decode duration, and total duration. Prompt throughput is authoritative prompt
tokens divided by prefill duration; decode throughput is authoritative output tokens divided by
decode duration. The report also records the request budget and `budget - total duration` completion
margin. Zero-duration boundaries are valid and retain a zero duration (their division-based
throughput is `null`); absent, non-finite, reversed, or over-budget timing fails closed and is never
coerced to zero.

Cancellation scenarios must be progress-triggered, not sleep-only:

```bash
python scripts/p8_benchmark.py packaged-runtime \
  --model /path/to/local-model.gguf \
  --backend metal \
  --relay-url http://127.0.0.1:8000 \
  --request-timeout 600 \
  --cleanup-timeout 30 \
  --out-dir .tmp/p8-prefill-cancel \
  --report-only
```

A future repository-owned physical runner should trigger cancellation during prefill after a configured processed-token
threshold or percentage, then trigger cancellation during generation after a configured generated-token
threshold. Each scenario must assert cancellation acknowledgement, prompt progress termination,
bounded cleanup, late-result suppression, stale-progress rejection, a successful small follow-up
request on a clean worker, and operator Stop/Start functionality afterward.

## P7 memory-estimator comparison

The harness consumes P7 estimator output; it does not duplicate the estimator formulas. Exact KV
comparison uses the estimator's `exact_kv_allocation_bytes` (or stable exact KV cache byte surface)
and compares it to llama.cpp/GGML runtime diagnostics such as `kv_allocation_bytes`. The default
alignment rule allows at most one 4 KiB page of difference. If exact comparison is requested and the
estimator used a conservative fallback, or runtime diagnostics are missing/ambiguous, the comparison
fails closed. RSS, VRAM, and unified-memory probes are recorded as noisy optional observations with
methodology rather than byte-for-byte assertions.

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
python -m pytest -q tests/unit/test_p8_benchmark_harness.py
python -m pytest -q tests/unit
pre-commit run --all-files
git diff --check
./run_all_tests.sh PR
```

Physical Metal/CUDA validation is manual and should attach only sanitized reports to #1566, #1608,
or P9. Do not claim 0.1.12 release validation or general semantic correctness from a report-only
baseline.
