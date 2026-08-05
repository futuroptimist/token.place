# P8 packaged-runtime benchmark harness

The P8 harness is a privacy-safe manual benchmark surface for issue #1566. It generates deterministic synthetic long-context fixtures, evaluates strict semantic JSON responses, validates progress and cancellation invariants with testable adapters, and writes sanitized versioned reports suitable for P9 comparisons.

Ordinary CI exercises the harness with deterministic canned events and responses only; it does not download models, require a GPU, start a packaged desktop app, or run multi-minute inference.

## Prerequisites for physical packaged-runtime runs

- Latest `main` containing PR #1612 (`cbc986d3ef51015c6660d3b367c55ba63392440e`) in history.
- A locally installed packaged desktop build with the bundled compute-node Python bridge.
- The existing pinned desktop runtime (`llama-cpp-python==0.3.32`); do not upgrade it for P8.
- macOS Apple Silicon with Metal runtime support or Windows NVIDIA/CUDA runtime support. CPU can be used only where the packaged runtime already supports it.
- The Qwen3 8B Q4_K_M GGUF model artifact already provisioned locally.

Packaged mode fails closed when required inputs are absent; it never substitutes a fake runtime.

## Fixture generation

Generate reproducible synthetic fixtures without committing prompt blobs:

```bash
python scripts/p8_benchmark.py generate-fixture --size small-8k --output-dir /tmp/token-place-p8
python scripts/p8_benchmark.py generate-fixture --size intermediate-32k --output-dir /tmp/token-place-p8
python scripts/p8_benchmark.py generate-fixture --size long-55k --output-dir /tmp/token-place-p8
```

The fixture manifest records schema version, seed, prompt hash, requested and actual token counts, early/middle/late target depths, expected answers, and scoring rules. When an authoritative packaged tokenizer adapter is available, use it for admission-equivalent counts; otherwise the fallback count is explicitly marked non-authoritative.

## Semantic evaluation modes

Strict mode returns nonzero when exact semantic correctness fails:

```bash
python scripts/p8_benchmark.py eval-response \
  --manifest /tmp/token-place-p8/synthetic-long-55k.manifest.json \
  --response /tmp/token-place-p8/model-response.json \
  --output-dir /tmp/token-place-p8
```

Report-only baseline mode records failures without relabeling them as passes:

```bash
python scripts/p8_benchmark.py eval-response \
  --manifest /tmp/token-place-p8/synthetic-long-55k.manifest.json \
  --response /tmp/token-place-p8/model-response.json \
  --output-dir /tmp/token-place-p8 \
  --report-only
```

Semantic sub-scores include JSON-only formatting, exact key set, canary retrieval, chapter selection, prose-versus-heading selection, exact whitespace word count, capitalization, trailing punctuation, and complete exact match. The known `VII` six-word response and `XIV`/`XXI` title substitutions fail strict evaluation.

## Packaged-runtime benchmark examples

Preflight a packaged bridge and write a sanitized report:

```bash
python scripts/p8_benchmark.py run \
  --runtime packaged \
  --bridge /Applications/token.place.app/Contents/Resources/python/compute_node_bridge.py \
  --model /path/to/Qwen3-8B-Q4_K_M.gguf \
  --output-dir /tmp/token-place-p8 \
  --timeout-seconds 30
```

Use the generated 8K, 32K, and 55K fixtures as the prompt inputs for full local manual runs through the existing API v1 E2EE request, encrypted progress, cancellation, and response paths. The stable report schema is `p8-benchmark-report/v1` and records runtime identity, safe model fingerprint, backend selection/usage, context tier, prompt/output token counts, batch profile, KV types, Flash Attention, KQV offload, layer offload, YaRN/RoPE settings, phase timings, throughput, progress invariants, request budget, cancellation timing, worker recovery timing, semantic trial summaries, and memory-comparison results when available.

## Cancellation and recovery scenarios

Use progress events, not sleeps, to trigger cancellation:

```bash
python scripts/p8_benchmark.py run --runtime packaged \
  --bridge /Applications/token.place.app/Contents/Resources/python/compute_node_bridge.py \
  --model /path/to/Qwen3-8B-Q4_K_M.gguf \
  --output-dir /tmp/token-place-p8-prefill-cancel
```

Manual cancellation validation should cover cancellation during prefill after a configured processed-token threshold and during generation after a generated-token threshold. Each scenario must assert cancellation acknowledgement, prompt progress termination, bounded cleanup, late-result suppression, stale-progress rejection, a successful small follow-up request on a clean worker, and operator Stop/Start functionality afterward.

## Memory-estimator comparison

The harness consumes P7 estimator breakdowns from `utils.llm.model_manager._qwen_64k_memory_estimate` rather than duplicating formulas. Exact comparison is allowed only when the estimator reports an exact KV allocation and packaged llama.cpp/GGML diagnostics report an unambiguous KV allocation. Conservative fallbacks are recorded but never relabeled as exact comparisons. RSS, VRAM, and unified-memory probes are optional noisy observations and should include methodology and tolerance notes.

## Report privacy and exit codes

Reports and normal logs must not contain prompt bodies, response bodies, ciphertext, IVs, keys, cancellation tokens, high-cardinality request IDs, absolute user paths, secrets, or unbounded subprocess output. Use fixture IDs, hashes, categorical error codes, bounded basenames, and aggregate scores.

Exit codes:

- `0`: success, or report-only semantic baseline completed.
- `2`: strict semantic, cancellation, recovery, or invariant failure.
- `3`: invalid inputs or missing packaged prerequisites.
- `4`: packaged runtime unavailable or failed bounded preflight.

Attach sanitized reports to #1566, #1608, or P9 only after confirming no prompt, response, ciphertext, secret, or absolute-path data is present.

## CI versus physical hardware

CI runs fixture generation, manifest validation, semantic scoring, progress invariant checks, memory comparison boundaries, cancellation-state adapter behavior, report redaction, schema validation, and CLI input validation using canned deterministic data. Physical Metal/CUDA runs remain manual and must be reported honestly as completed or not run.
