# 2026-05-24: Desktop macOS Start Operator regression + e2e gap

## Summary
Two desktop regressions affected the Tauri compute-node operator flow:
1. First launch prefilled **Model GGUF path** from runtime artifact resolution instead of leaving it blank.
2. **Start operator** could flip `Running` to `yes` briefly and then back to `no` on startup failure without a visible, actionable UI error.

## User impact
- First-launch UX showed a non-user-selected model path, including potentially platform-specific paths.
- On macOS startup failures, users saw an apparent transient start with no immediate actionable feedback.

## Symptoms
- Model path input populated on initial load before user browse/select action.
- Clicking **Start operator** could show `Running: yes` transiently, then return to `Running: no`.
- Failure reason was not reliably visible at the moment startup failed.

## Root causes
- Frontend initialization path set and persisted `config.model_path` from `inspect_model_artifact().resolved_model_path` when saved config was blank.
- Frontend start handler optimistically set compute-node `running: true` before backend startup confirmation/event.

## Why existing coverage missed it
- UI tests covered event-driven error transitions but did not enforce first-launch blank path behavior.
- UI e2e validated a successful start path but did not assert startup-state stability after the initial running transition.
- UI e2e was exercised on Linux CI; macOS packaged coverage existed, but macOS UI wiring parity was not validated by the same UI e2e flow.

## Fix implemented
- Removed auto-population/persistence of model path during app initialization.
- Updated start-operator UI handling to avoid optimistic `running: true`; running now remains `no` until backend events confirm live state.
- Strengthened UI e2e to assert blank initial model input and stable running state after start.
- Enabled macOS CI execution of desktop operator UI e2e.

## Tests added/updated
- Updated desktop React tests for first-launch blank model path and no implicit save.
- Updated UI e2e to assert first-launch blank model path, stable running state, and capture screenshots on failure.
- Added macOS UI e2e job in CI workflow.

## Follow-up
- Monitor macOS UI e2e runtime and flake rate; split test phases if runtime becomes a bottleneck.
