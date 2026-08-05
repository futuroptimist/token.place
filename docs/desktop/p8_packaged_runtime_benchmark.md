# P8 packaged-runtime benchmark harness

The P8 harness provides privacy-safe fixtures, strict semantic scoring, progress
validation, cancellation/recovery contracts, and sanitized report generation for
issue #1566. Ordinary CI uses deterministic adapters and does not download a
model, require a GPU, or run multi-minute inference.

## Prerequisites for physical packaged runs

- Build or install the token.place desktop app for the platform under test.
- macOS Metal: Apple Silicon with the packaged `llama-cpp-python==0.3.32` Metal
  runtime and Qwen3 8B Q4_K_M model already available locally.
- Windows CUDA: NVIDIA GPU with CUDA-capable packaged `llama-cpp-python==0.3.32`
  runtime and the same model/quantization.
- CPU is supported only where the packaged desktop runtime already supports it.

The harness never substitutes a fake runtime in packaged mode. Missing app,
model, hardware, backend support, or required telemetry is a failure.

## Fixture generation

```bash
python scripts/p8_benchmark.py generate-fixture --tier small-8k --out-dir .p8-fixtures
python scripts/p8_benchmark.py generate-fixture --tier intermediate-32k --out-dir .p8-fixtures
python scripts/p8_benchmark.py generate-fixture --tier 64k-full --out-dir .p8-fixtures
```

Fixtures are generated from `p8-semantic-fixture.v1` and a deterministic seed.
The manifest records requested tokens, actual tokens, the tokenizer source,
fixture SHA-256, target depths, expected answers, and scoring rules. If a
packaged runtime tokenizer adapter is available, use it for admission-compatible
actual token counts; otherwise generated unit-test fixtures clearly identify the
whitespace tokenizer.

## Semantic modes

Strict mode exits nonzero on exact-match failure. Report-only mode records the
same sub-scores without relabeling failures as passes, which is useful for P8/P9
baselines where Qwen3 8B may fail semantic exactness.

```bash
python scripts/p8_benchmark.py evaluate --manifest .p8-fixtures/small-8k.manifest.json --response canned-good.json --strict
python scripts/p8_benchmark.py evaluate --manifest .p8-fixtures/small-8k.manifest.json --response observed-bad.json
```

Sub-scores cover JSON-only output, exact key set, canary retrieval, target
selection, prose-versus-heading selection, exact whitespace word count,
capitalization, trailing punctuation, and complete exact match.

## Packaged-runtime benchmark examples

```bash
python scripts/p8_benchmark.py run-packaged --app-binary /path/to/token.place.app --model /path/to/qwen3-q4_k_m.gguf --backend metal --out-dir .p8-reports
python scripts/p8_benchmark.py run-packaged --app-binary 'C:\\Path\\token.place.exe' --model 'D:\\models\\qwen3-q4_k_m.gguf' --backend cuda --out-dir .p8-reports
```

Representative contexts are `small-8k`, `intermediate-32k`, and `64k-full`
(about 55K prompt tokens inside the 64K tier). Expected 64K physical runs can
consume several minutes; keep the request budget at or above 480 seconds when
reproducing the P7 baseline.

## Cancellation examples

Cancellation must be triggered by progress events, not fixed sleeps:

```bash
python scripts/p8_benchmark.py run-packaged --app-binary /path/to/token.place.app --model /path/to/model.gguf --backend metal --out-dir .p8-reports
```

Physical scenarios should cancel once prefill reaches a configured processed
count or percentage, and once generation reaches a configured generated-token
count. The report records acknowledgement, prompt progress termination, bounded
cleanup, late-result suppression, successful small follow-up request, and
operator Stop/Start health.

## Report schema and exit codes

Reports use `p8-benchmark-report.v1`, are written atomically, and are safe to
attach to #1566, #1608, or P9 only after reviewing that no opt-in debug artifact
contains prompt/response bodies. Normal reports contain fixture IDs and hashes,
low-cardinality runtime/build fields, benchmark settings, metrics, semantic
category results, cancellation outcomes, and memory comparison results.

Exit codes:

- `0`: command succeeded; in report-only mode semantic failures may be present.
- `1`: strict semantic, cancellation, recovery, progress, telemetry, or memory
  invariant failed.
- `2`: CLI usage error.

## Memory comparison

The harness consumes P7 estimator output from `utils.llm.model_manager` rather
than duplicating formulas. Exact KV allocation is compared with packaged
llama.cpp/GGML KV allocation diagnostics using a small backend log-rounding
alignment tolerance. Conservative fallbacks, missing diagnostics, ambiguous KV
logs, RSS, VRAM, and unified-memory probes are recorded separately and are not
relabelled as exact byte-for-byte comparisons.

## CI versus physical hardware

Ordinary CI runs unit and contract tests with canned progress/response streams.
It validates fixture determinism, strict semantic failure categories, metrics,
KV-comparison boundaries, memory-adapter sanitization, cancellation contracts,
report redaction, and fail-closed packaged mode. Physical Metal/CUDA benchmarks
remain explicit local/manual validation and should be attached as sanitized JSON
reports only when genuinely run.
