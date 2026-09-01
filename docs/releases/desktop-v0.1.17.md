# token.place desktop v0.1.17 candidate record

This record freezes the immutable candidate identity and the Step 05 staging handoff. It does not
claim that staging, production, Windows CUDA, or macOS Metal qualification has passed. Use the
evergreen [production promotion checklist](../PRODUCTION_PROMOTION.md) for the later cutover.

## Immutable candidate identity

- Desktop version/tag: `0.1.17` / `desktop-v0.1.17`.
- Source commit: `8618c9aba4b5dfe7980c2fe861095a92311145f2`. The relay and desktop must both
  come from this commit; a source mismatch invalidates the candidate.
- Relay images: `ghcr.io/futuroptimist/tokenplace-relay:main-8618c9a` and
  `ghcr.io/futuroptimist/tokenplace-relay:sha-8618c9a`.
- Relay index digest:
  `sha256:b32ef19840dabe44caf7240b787af14dcf439b39357833e21a92c3dd511effd4`.
- Helm chart package/app versions: `0.1.4` / `0.1.1`.
- Model ID/profile/file: `qwen3-8b-instruct` / `qwen3-8b-q4-k-m` /
  `Qwen3-8B-Q4_K_M.gguf`.
- Model SHA-256: `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
- Windows setup: `token.place.desktop_0.1.17_x64-setup.exe`; SHA-256
  `02ccaa3916e2ad7d07445eeaca2344ac90a99c217ac343765d8c5caecd910b73`.
- Windows MSI: `token.place.desktop_0.1.17_x64_en-US.msi`; SHA-256
  `62aa63885e769a48064d7100022dc4a483eb0177188844cfac20a318aa4221eb`.
- macOS Apple Silicon DMG: `token.place-desktop-0.1.17-apple-silicon.dmg`; SHA-256
  `8257b96ec81d9025283466881ca20eabf54aed2a48cd7303279a8b08d2e656f6`.
- Release: [desktop-v0.1.17](https://github.com/futuroptimist/token.place/releases/tag/desktop-v0.1.17).

API v1 remains the only active runtime target and is non-streaming: a response is returned only
after complete model output generation. All distributed inference must remain relay-blind E2EE.
Relay transport and relay-owned operational state may handle only ciphertext envelopes plus safe
routing metadata, and processing must fail closed if E2EE cannot be preserved. Logs, diagnostics,
and Step 05 evidence must retain no plaintext, keys, ciphertext bodies, prompts, or responses.
Packaging CI establishes build evidence, not physical Windows or macOS qualification.

## Step 05 staging handoff and evidence template

Step 05 must be performed on isolated real hosts and recorded without asserting success in advance:

1. Deploy immutable relay tag `main-8618c9a` to staging and verify that the live image resolves to
   the relay index digest above. Record deployment timestamp, relay build ID, full image tag and
   digest, and chart package/app versions.
2. Verify only the downloaded installer and model hashes against the frozen immutable values in
   this record before installation. Record artifact names, architectures, hashes, and model
   ID/profile/file. After finalizing the evidence bundle, calculate and record its SHA-256; this new
   evidence-bundle checksum is not compared with the immutable candidate hashes.
3. On an isolated real Windows 11 CUDA host, install the candidate and record hardware, driver,
   detected CUDA backend, desktop build ID, start/finish timestamps, and latency. Separately repeat
   on an isolated real macOS Apple Silicon host, recording hardware, OS, detected Metal backend,
   build ID, timestamps, and latency.
4. Configure **each Step 05 desktop only for `https://staging.token.place`**. Production still
   advertises Llama, so no Qwen candidate desktop may register with `https://token.place` before the
   coordinated Step 06 cutover.
5. Through each host, complete one encrypted, non-streaming Qwen chat. Validate the staging relay
   landing page and API v1 model identity and envelope flow; do not use API v2, streaming, or legacy
   relay routes.
6. With both nodes registered, capture two-node round-robin selection, per-chat stickiness, and
   failover after the sticky node is made unavailable. Then stop work and prove the queue and
   in-flight counts drain to zero (or document a fail-closed terminal outcome).
7. Preserve timestamps, desktop and relay build IDs, immutable tags/digests, artifact and model
   hashes, evidence-bundle checksum, per-host latency, architecture, hardware, and confirmed
   acceleration backend. Evidence must contain **no plaintext, keys, ciphertext bodies, prompts,
   or responses**.

## Production rollback gate

The known production rollback baseline is relay
`ghcr.io/futuroptimist/tokenplace-relay:sha-dc6ac09`, chart/app `0.1.4` / `0.1.1`, and model
`llama-3.1-8b-instruct`. The rollback desktop identity is not yet known and must not be invented.

Step 06 remains blocked until the exact currently installed production desktop version, installer,
checksum, architecture, and model artifact are captured directly from **every** production compute
host and preserved. Rollback must restore relay, desktop, and model together; a mixed Llama/Qwen
fleet is forbidden.
