# P8 packaged-runtime benchmark harness

This harness creates privacy-safe synthetic long-context fixtures and records sanitized P8 reports for comparisons in #1566, #1608, and P9. It does not tune models or runtime defaults.

## What ordinary CI covers

Ordinary tests use deterministic adapters and do **not** require a model download, GPU, network, installed desktop app, or multi-minute inference. They validate fixture generation, semantic scoring, progress invariants, cancellation/recovery state handling, memory-estimator comparison logic, report redaction, atomic writes, and CLI fail-closed behavior.

## Packaged-runtime prerequisites

Packaged mode is explicit and fail-closed. Use it only on a machine with the packaged desktop compute-node runtime, the Qwen3 8B Q4_K_M GGUF artifact, and the requested backend:

- macOS Apple Silicon: packaged app with Metal-enabled `llama-cpp-python==0.3.32`.
- Windows NVIDIA: packaged app with CUDA-enabled `llama-cpp-python==0.3.32`.
- CPU: only where the packaged runtime already supports CPU for the selected context tier.

The harness must never silently replace packaged mode with a fake runtime.

## Fixture generation

```bash
python scripts/p8_packaged_runtime_benchmark.py generate-fixture --tier 8k --output-dir ./p8-fixtures
python scripts/p8_packaged_runtime_benchmark.py generate-fixture --tier 32k --output-dir ./p8-fixtures
python scripts/p8_packaged_runtime_benchmark.py generate-fixture --tier 55k --output-dir ./p8-fixtures
```

Each fixture writes a prompt text file and a separate manifest/oracle containing the fixture version, seed, SHA-256 prompt hash, requested and actual token counts, early/middle/late target depths, expected answers, and scoring rules. When a packaged tokenizer adapter is available, use it to verify counts against the same tokenizer used for admission; CI uses a deterministic surrogate only for harness tests.

## Semantic modes

Strict mode returns nonzero for semantic failure. Report-only mode records failures without relabeling them as passes.

```bash
python scripts/p8_packaged_runtime_benchmark.py evaluate --manifest ./p8-fixtures/synthetic-55k.manifest.json --response ./response.json --strict
python scripts/p8_packaged_runtime_benchmark.py evaluate --manifest ./p8-fixtures/synthetic-55k.manifest.json --response ./response.json
```

Sub-scores include valid JSON without Markdown/commentary, exact key set, canary retrieval, target/chapter selection, prose-versus-heading selection, exact whitespace-separated word counts, capitalization preservation, trailing-punctuation rules, and complete exact match. The known P7 baseline failures (`VII` six-word answer and `XIV`/`XXI` chapter-title substitutions) are strict failures.

## Benchmark examples

CI-safe fake mode creates a representative schema-valid report:

```bash
python scripts/p8_packaged_runtime_benchmark.py run --mode fake --tier 8k --output-dir ./p8-reports --strict
python -m json.tool ./p8-reports/p8-runtime-benchmark-report.json >/dev/null
```

Physical packaged-runtime examples must point at a real packaged app and should be run only when prerequisites are genuinely present:

```bash
python scripts/p8_packaged_runtime_benchmark.py run --mode packaged --tier 8k --output-dir ./p8-reports/8k --packaged-app "/Applications/token.place desktop.app" --strict
python scripts/p8_packaged_runtime_benchmark.py run --mode packaged --tier 32k --output-dir ./p8-reports/32k --packaged-app "/Applications/token.place desktop.app" --strict
python scripts/p8_packaged_runtime_benchmark.py run --mode packaged --tier 55k --output-dir ./p8-reports/55k --packaged-app "/Applications/token.place desktop.app"
```

## Cancellation examples

The cancellation harness triggers cancellation from progress events rather than fixed sleeps:

```bash
python scripts/p8_packaged_runtime_benchmark.py run --mode packaged --tier 55k --output-dir ./p8-reports/cancel-prefill --packaged-app "/Applications/token.place desktop.app" --strict
python scripts/p8_packaged_runtime_benchmark.py run --mode packaged --tier 8k --output-dir ./p8-reports/cancel-generation --packaged-app "/Applications/token.place desktop.app" --strict
```

For each scenario, validate cancellation acknowledgement, prompt progress termination, bounded worker cleanup, late-result suppression, stale-progress rejection, successful clean-worker follow-up, and operator Stop/Start functionality.

## Report schema and exit codes

Reports use schema `p8-runtime-benchmark-report/v1`. They include fixture identity and hash, sanitized runtime identity/diagnostics, effective generation settings, semantic category results, progress invariants, phase timings, throughput, request budget/margin, cancellation/recovery fields, and privacy flags. Reports must not include prompt or response bodies, ciphertext, IVs, keys, cancellation tokens, high-cardinality IDs, absolute user paths, secrets, or unbounded subprocess output.

Exit codes:

- `0`: command completed and strict checks passed, or report-only mode recorded failures.
- `2`: strict semantic/correctness failure.
- `3`: packaged-runtime prerequisites absent or ambiguous.

## Memory comparison

The harness consumes P7's `_qwen_64k_memory_estimate` breakdown rather than duplicating formulas. Exact KV comparison is allowed only when P7 reports an exact allocation and no conservative fallback. Runtime llama.cpp/GGML KV allocation diagnostics must be present and are compared with a 16 MiB tolerance for allocator/page reporting granularity. RSS, VRAM, and unified-memory probes are optional, sanitized, bounded, and reported as noisy observations rather than byte-for-byte assertions.

## Attaching reports

Attach only sanitized JSON reports and the concise summary to #1566, #1608, or P9. Do not attach generated prompts, model responses, local paths, model artifacts, or machine-specific raw logs unless an explicit safe generated-fixture artifact mode is added and reviewed.
