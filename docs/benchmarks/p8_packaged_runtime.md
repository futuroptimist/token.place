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

The harness never silently substitutes a fake runtime or trusts an HTTP adapter for
packaged-runtime mode:

```bash
python scripts/p8_benchmark.py packaged-runtime --out-dir .tmp/p8
```

This currently exits nonzero. The installed desktop exposes operator Start/Stop only through its
in-process Tauri command surface; it has no authenticated external benchmark-control seam that can
select a model, observe the API v1 encrypted progress lifecycle, trigger cancellation, and return
bounded evidence to this Python harness. Directly running the bridge with system Python would not
exercise the installed package, while accepting a caller-provided HTTP endpoint would send the
plaintext fixture and model path to an unverifiable process. Both substitutes are therefore
rejected. A physical command must not be documented as successful until the desktop owns such a
control seam (which is outside this task's allowed production-code scope).

Before that seam can be connected, inputs are still validated fail closed: the model must be a
readable regular file, the backend must be recognized, relay/control HTTP endpoints must resolve to
loopback, and request/cleanup timeouts must be finite and positive. Unit tests may inject a callable
runner directly into the Python API to verify orchestration and evidence validation, but that hook
is intentionally unavailable from the production CLI and is not physical evidence.

## Fixture generation

Generate deterministic, synthetic, privacy-safe fixtures instead of committing large prompt blobs:

```bash
python scripts/p8_benchmark.py generate-fixture --fixture small-8k --out-dir .tmp/p8-fixtures
python scripts/p8_benchmark.py generate-fixture --fixture intermediate-32k --out-dir .tmp/p8-fixtures
python scripts/p8_benchmark.py generate-fixture --fixture long-55k --out-dir .tmp/p8-fixtures
```

The fixture manifest records the fixture version, deterministic seed, requested token count, actual
CI-tokenizer count or adapter-tokenizer count, prompt SHA-256, target depths, expected answers, and
scoring rules. When a packaged-runtime tokenizer adapter is available, use it to verify admission
counts; otherwise ordinary CI uses the deterministic whitespace tokenizer and clearly labels it as
`whitespace-ci`.

The current synthetic fixture IDs are:

| Fixture | Requested size | Purpose |
| --- | ---: | --- |
| `small-8k` | 8,192 tokens | fast unit/contract validation |
| `intermediate-32k` | 32,768 tokens | mid-depth haystack validation |
| `long-55k` | 55,254 tokens | approximate `64k-full` benchmark comparable to #1566 |

Each fixture plants early, middle, and late chapter targets, repeated decoys, table-of-contents
ambiguity, prose-versus-title traps, exact canary retrieval, JSON-only output, exact-key-set,
capitalization, punctuation, and five-word extraction rules.

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
removed or redacted.

Physical run adapters should populate these low-cardinality fields when available:

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

Progress contract checks use the production phases `preparing`, `prefill`, and `generating`. Completion, cancellation, and failure are derived from the response/control lifecycle rather than from a required terminal progress event. Checks reject decreasing sequence numbers, decreasing processed/generated/elapsed counters, changing prompt totals, cached counts above processed counts, processed counts above total, invalid phase transitions, post-terminal/stale progress reported by the adapter lifecycle, and late results after cancellation.

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
- `2`: invalid CLI input or missing `--adapter-url` before packaged-runtime prerequisite checks.

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
