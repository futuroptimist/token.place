# Installed GPU completion preflight

Issue [#1854](https://github.com/futuroptimist/token.place/issues/1854) adds an opt-in,
installed-artifact qualification boundary:

```text
token-place-desktop-tauri --installed-gpu-completion-preflight
```

This command performs **real GPU inference**. Run it only after the operator has received explicit
qualification authorization. The command neither grants nor consumes a benchmark attempt, and it
does not contact a relay, register capacity, perform a benchmark handshake, or change benchmark
counters. Normal application startup and installation never invoke it.

## Boundary and budgets

The desktop executable resolves the production bundled Python interpreter, packaged
`compute_node_bridge.py`, and approved model metadata using the same resource and model bridges as
ordinary installed startup. System-Python substitution, a missing or differently named approved
model, mock inference, and CPU fallback fail closed. The bridge selects the production GPU mode
(`cuda` on supported Windows NVIDIA installations or `metal` on supported Apple Silicon
installations), creates the shared `ComputeNodeRuntime`, and calls the model manager's
non-streaming `create_chat_completion_with_recovery` child-worker path exactly once. It deliberately
does not call the API-v1 readiness warm-up because that warm-up performs its own completion.

The fixed synthetic message asks for one short word. Generation is non-reasoning, atomic, and
limited to 16 tokens; accepted UTF-8 output must be nonempty and no larger than 4,096 bytes. Its
text is never included in evidence or diagnostics. Defaults are intentionally conservative relative
to existing desktop warm-load and cleanup conventions:

| Phase | Deadline |
| --- | ---: |
| dependency/startup | 15 seconds |
| model load and child startup | 180 seconds |
| the single generation | 60 seconds |
| cancellation after generation timeout | 5 seconds |
| owned-worker cleanup | 10 seconds |
| total | 240 seconds |

Every outcome attempts teardown of only the runtime and child worker created by this invocation.
Acceptance requires verified cleanup. A deadline, worker exit, malformed or oversized event,
duplicate completion, fallback, cancellation failure, or cleanup failure returns nonzero.

## Evidence

Standard output contains one versioned JSON object. Its allowlisted schema contains:

- application version, build ID, target triple, and bundled runtime ID;
- approved model profile, filename, and identity result;
- declared GPU configuration separately from observed `cuda`/`metal` execution evidence;
- phase outcomes and elapsed milliseconds, configured limits, completion count/byte count, and
  cleanup result; and
- a stable failure code and final `accepted` boolean.

It never contains the prompt, generated text, ciphertext, keys, credentials, environment dumps,
filesystem paths, relay payloads, or raw child logs.

## Qualification layers

1. **Source-level tests** use controlled protocol fixtures and do not establish CUDA or Metal
   qualification.
2. **Signed-release qualification** confirms the exact installer, app, packaged runtime, and model
   identities; it does not prove a completion.
3. **GPU completion qualification** is this explicitly authorized command against that exact signed
   installation. A successful evidence object proves one bounded local completion and cleanup.
4. **Benchmark authorization** remains separate. This command cannot authorize or consume a physical
   benchmark attempt.

Do not describe source tests, mocks, an unsigned local build, or historical evidence as physical GPU
qualification.
