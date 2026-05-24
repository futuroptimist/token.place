# 2026-05-24 desktop macOS start-operator regression and e2e gap

## Summary
The desktop Tauri operator UI regressed in two ways: first-launch model path displayed a machine-
specific value, and operator start failures on macOS could present as a brief `Running: yes` flip
without a visible UI error.

## User impact
- New users could see a misleading pre-filled GGUF path before selecting a model.
- macOS users could click **Start operator** and see it revert to `Running: no` without clear
  in-app failure context.

## Symptoms
- Initial load could populate **Model GGUF path** from runtime artifact resolution rather than
  preserved user config.
- **Start operator** could transiently show running status and then exit, with diagnostics only in
  backend logs/events.

## Root causes
1. UI initialization auto-saved `inspect_model_artifact().resolved_model_path` into
   `config.model_path` whenever persisted `model_path` was blank.
2. UI set `running: true` optimistically before confirmed bridge startup events, and event-driven
   operator errors were not mirrored into the global visible error banner.

## Why existing e2e missed this (especially on macOS)
- The desktop UI e2e test explicitly writes a temporary model path before start and did not assert
  first-launch blank state.
- The test asserted `Running: yes` once, but did not validate that state remained stable for a
  post-start window, so quick start/exit transitions could evade detection.

## Fix implemented
- Removed auto-population/auto-save of model path from runtime artifact inspection when config is
  blank.
- Updated start behavior to avoid optimistic `running: true` before backend status events confirm
  startup.
- Propagated compute-node error events to a visible UI error message.
- Strengthened desktop UI e2e to assert blank initial model input and stable `Running: yes` after
  Start operator.

## Tests added/updated
- Updated React unit tests to verify:
  - first-launch blank config remains blank even if runtime resolved path is Windows-style,
  - persisted model path is restored,
  - operator start failures are surfaced.
- Updated desktop UI e2e to verify blank initial path and running-state stability.

## Follow-up
- Current CI limitation: `.github/workflows/desktop-operator-e2e.yml` runs
  `test_desktop_operator_ui_e2e.py` only on Linux because it depends on `tauri-driver` +
  WebKitGTK/Xvfb orchestration that is currently provisioned only in the Ubuntu job.
- Strongest automated macOS substitute currently in CI is
  `desktop-operator-packaged-e2e-macos`, which runs
  `desktop-tauri/scripts/test_packaged_operator_e2e.py` (including inspect-only and full packaged
  bridge coverage) to validate macOS operator startup/bridge behavior.
- When macOS Tauri UI WebDriver plumbing is added to CI, add an explicit macOS job for
  `test_desktop_operator_ui_e2e.py` without weakening the blank-initial-path or running-stability
  assertions.
