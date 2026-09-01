# token.place desktop-v0.1.17 candidate record

This record freezes the Step 04 candidate identity and the Step 05 staging handoff. It does not
claim physical desktop, staging, or production qualification; use the evergreen
[promotion checklist](../PRODUCTION_PROMOTION.md) for the complete promotion procedure.

## Immutable candidate identity

- Desktop version/tag: `0.1.17` / `desktop-v0.1.17`.
- Source commit: `8618c9aba4b5dfe7980c2fe861095a92311145f2`.
- Relay images: `ghcr.io/futuroptimist/tokenplace-relay:main-8618c9a` and
  `ghcr.io/futuroptimist/tokenplace-relay:sha-8618c9a`.
- Relay index digest:
  `sha256:b32ef19840dabe44caf7240b787af14dcf439b39357833e21a92c3dd511effd4`.
- Helm chart package/app versions: `0.1.4` / `0.1.1`.
- Model ID/profile/file: `qwen3-8b-instruct` / `qwen3-8b-q4-k-m` /
  `Qwen3-8B-Q4_K_M.gguf`.
- Model SHA-256: `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`.
- Windows setup: `token.place.desktop_0.1.17_x64-setup.exe`, SHA-256
  `02ccaa3916e2ad7d07445eeaca2344ac90a99c217ac343765d8c5caecd910b73`.
- Windows MSI: `token.place.desktop_0.1.17_x64_en-US.msi`, SHA-256
  `62aa63885e769a48064d7100022dc4a483eb0177188844cfac20a318aa4221eb`.
- macOS Apple Silicon DMG: `token.place-desktop-0.1.17-apple-silicon.dmg`, SHA-256
  `8257b96ec81d9025283466881ca20eabf54aed2a48cd7303279a8b08d2e656f6`.
- Release: [desktop-v0.1.17](https://github.com/futuroptimist/token.place/releases/tag/desktop-v0.1.17).

The relay and desktop must both originate from the candidate source commit above. API v1 remains
the only active runtime target, is non-streaming, and returns only after full model generation.
Distributed inference must remain relay-blind E2EE: relay-owned state, logs, diagnostics, and
payloads contain ciphertext only plus safe routing metadata and fail closed if E2EE cannot be
preserved. Packaging CI establishes artifact provenance, not physical Windows CUDA or macOS Metal
qualification.

## Step 05 staging evidence template

Step 05 must deploy the immutable `main-8618c9a` relay tag to staging and verify that the live image
resolves to the relay index digest above. Configure candidate desktops **only** for
`https://staging.token.place`. Production still advertises Llama, so Qwen candidate desktops must
not register with `https://token.place` before the coordinated Step 06 cutover.

Record, without claiming success until observed:

- [ ] Deployment timestamp, live relay image tag and digest, chart/app versions, source commit, and
      relay build ID.
- [ ] Locally calculated installer, model, and evidence-bundle SHA-256 checksums, matched to the
      immutable values above.
- [ ] An isolated real Windows 11 CUDA trial and an isolated real macOS Apple Silicon Metal trial,
      including timestamp, desktop build ID, installer name/hash, architecture, hardware/backend,
      model file/profile/ID/hash, and measured end-to-end latency.
- [ ] One encrypted, non-streaming Qwen chat through each host, plus relay landing-page and API v1
      validation.
- [ ] Both-node round-robin, per-chat stickiness, and failover evidence, followed by confirmation
      that queue and in-flight work drain.
- [ ] Final evidence-bundle checksum and a review confirming the evidence contains no plaintext,
      keys, ciphertext bodies, prompts, or responses.

## Rollback baseline and Step 06 blocker

The known production rollback baseline is relay
`ghcr.io/futuroptimist/tokenplace-relay:sha-dc6ac09`, chart/app `0.1.4` / `0.1.1`, and model
`llama-3.1-8b-instruct`. The rollback desktop identity is not yet known and must not be inferred.

Step 06 remains blocked until the exact currently installed production desktop version, installer,
checksum, architecture, and model artifact are captured directly from every production compute
host and preserved. Rollback must restore the relay, desktop, and model together; a mixed
Llama/Qwen fleet is forbidden.
