# Desktop macOS operator start regression and e2e gap (2026-05-24)

## Summary
A desktop regression allowed a machine-specific model path to appear in the "Model GGUF path" field on first launch, and operator startup failures could appear as a brief Running=yes flip followed by Running=no without a clear top-level UI error.

## User impact
- First-run UX was misleading because the model path field looked preconfigured even when the user had not chosen a GGUF file.
- On macOS, operator startup failures were easy to miss and looked like silent failure.

## Symptoms
- Initial load could show a non-user-selected runtime-resolved model path in the model input.
- Clicking **Start operator** could briefly show Running: yes and then revert to no.
- Failure diagnostics were not reliably surfaced in the visible UI error banner.

## Root causes
1. Frontend initialization auto-populated and persisted `model_path` from runtime artifact metadata when config was empty.
2. Frontend startup UI optimistically set `running=true` before a compute-node started event.
3. Compute-node event failures were only reflected in status text and not consistently surfaced in the explicit error banner.

## Why e2e did not catch it (macOS focus)
The desktop operator UI e2e test validated successful start after filling inputs, but it did not assert first-launch blank model-path behavior and did not enforce stable post-click running state beyond the first observed success transition.

## Fix implemented
- Removed first-launch auto-population/persistence of model path from artifact metadata.
- Changed operator start UI flow to avoid optimistic running=true before backend startup confirmation.
- Wired compute-node event `last_error`/`message` into the visible UI error banner.
- Updated desktop operator UI e2e to assert blank initial model path and stable running state after startup.

## Tests updated
- `desktop-tauri/src/App.test.tsx`
  - added first-launch blank model-path test
  - updated startup-failure UI assertions
- `desktop-tauri/scripts/test_desktop_operator_ui_e2e.py`
  - assert initial GGUF field is blank
  - assert running state remains stable after startup

## Follow-up
- Keep macOS desktop operator e2e in CI and retain failure diagnostics artifacts (UI screenshot + relay/driver logs).
